import requests
import time
import re
import logging
from typing import Dict, List

logger = logging.getLogger(__name__)

# Общие коды ошибок API smsc.ru (одинаковы для send.php/status.php/balance.php).
# "9" особенно важен: это не "номер недоступен", а защита от слишком частых
# одинаковых запросов - реальный статус номера тут вообще не определялся.
SMSC_ERROR_MESSAGES = {
    "1": "Ошибка в параметрах запроса",
    "2": "Неверный логин или пароль/API-ключ",
    "3": "Недостаточно средств на счёте",
    "4": "IP-адрес временно заблокирован из-за частых ошибок в запросах",
    "5": "Неверный формат даты",
    "6": "Абонент не найден / сообщение запрещено",
    "7": "Неверный формат номера телефона",
    "8": "Сообщение на указанный номер не может быть доставлено",
    "9": "Слишком частый повторный запрос по этому номеру - подождите минуту и попробуйте снова",
}


def describe_error(code: str) -> str:
    """Человекочитаемое описание кода ошибки smsc.ru (код передаётся со знаком минус, например '-9')."""
    bare = code[1:] if code.startswith("-") else code
    return SMSC_ERROR_MESSAGES.get(bare, f"Неизвестная ошибка (код {code})")


class SMSC_API:
    """
    Обёртка над HTTP API smsc.ru, использующая тот же формат ответа
    (fmt=1, значения через запятую), что и официальная Python-библиотека
    SMSC.ru — это единственный вариант, который гарантированно
    задокументирован и предсказуем (в отличие от текста без fmt,
    где ответ не содержит слов "OK"/"ID").
    """

    def __init__(self, login: str, api_key: str, debug: bool = True):
        self.login = login
        self.api_key = api_key
        self.base_url = "https://smsc.ru/sys/"
        self.debug = debug

    def _log_raw(self, label: str, text: str):
        if self.debug:
            logger.warning("[SMSC RAW %s] %s", label, text)
            print(f"[SMSC RAW {label}] {text}")

    def _auth_params(self) -> dict:
        # ВАЖНО: используем именно login+apikey (а не login+psw!) — эта
        # комбинация подтверждённо проходит авторизацию и списывает деньги
        # с этого аккаунта. psw ожидает пароль от личного кабинета, а не
        # API-ключ, поэтому login+psw давал ошибку 2 "неверный логин или пароль".
        if self.login:
            return {"login": self.login, "apikey": self.api_key}
        return {"apikey": self.api_key}

    def check_phone_hlr(self, phone: str) -> Dict:
        """Отправляет HLR-запрос и возвращает результат по номеру."""
        phone = re.sub(r'\D', '', phone)

        params = {
            **self._auth_params(),
            "fmt": 1,              # обычный текстовый формат "через запятую" (проверенный, из офиц. библиотеки)
            "charset": "utf-8",
            "cost": 3,             # запросить также стоимость и остаток баланса
            "phones": phone,
            "mes": "",             # для HLR текст сообщения не нужен, но параметр обязателен
            "translit": 0,
            "id": 0,
            "hlr": 1,
        }

        try:
            response = requests.get(f"{self.base_url}send.php", params=params, timeout=15)
            raw = response.text.strip()
            self._log_raw("send.php", raw)

            parts = raw.split(",")

            # Общий признак ошибки API у smsc.ru: ("0", "-код_ошибки", ...)
            if len(parts) >= 2 and parts[1].startswith("-"):
                return {
                    'phone': phone,
                    'status': f'Ошибка отправки: {describe_error(parts[1])}',
                    'code': parts[1],
                    'raw_send': raw,
                }

            if len(parts) < 2:
                return {
                    'phone': phone,
                    'status': 'Ошибка',
                    'code': f'Неожиданный формат ответа: {raw!r}',
                    'raw_send': raw,
                }

            msg_id = parts[0]

            # Даём SMSC время обработать HLR-запрос
            time.sleep(4)

            status_params = {
                **self._auth_params(),
                "fmt": 1,
                "charset": "utf-8",
                "phone": phone,
                "id": msg_id,
                "all": 0,
            }
            status_response = requests.get(f"{self.base_url}status.php", params=status_params, timeout=15)
            status_raw = status_response.text.strip()
            self._log_raw("status.php", status_raw)

            return self._map_status(status_raw, phone, msg_id)

        except requests.RequestException as e:
            return {
                'phone': phone,
                'status': 'Исключение',
                'code': str(e),
            }

    def _map_status(self, status_raw: str, phone: str, msg_id: str) -> Dict:
        """
        Разбирает ответ status.php.

        Формат по документации smsc.ru для HLR (fmt=1):
        <статус>,<время>,<код ошибки>,<IMSI>,<сервис-центр>,<код страны>,
        <код оператора>,<страна>,<оператор>,<роуминг-страна>,<роуминг-оператор>

        Ошибка API: "0,-<код_ошибки>"
        """
        parts = status_raw.split(",")

        if len(parts) == 2 and parts[1].startswith("-"):
            return {
                'phone': phone,
                'status': f'Ошибка получения статуса: {describe_error(parts[1])}',
                'code': parts[1],
                'raw_status': status_raw,
            }

        def get(i):
            return parts[i] if len(parts) > i else None

        status_code = get(0)
        result = {
            'phone': phone,
            'id': msg_id,
            'status_code': status_code,
            'time': get(1),
            'err': get(2),
            'imsi': get(3),
            'service_center': get(4),
            'country_code': get(5),
            'operator_code': get(6),
            'country_name': get(7),
            'operator_name': get(8),
            'roaming_country': get(9),
            'roaming_operator': get(10),
            'raw_status': status_raw,
        }

        # На реальных данных подтверждено:
        # status_code=1 & err=0  -> абонент найден (номер существует у оператора)
        # status_code=20 & err=6 -> абонент не найден (номер не существует)
        # operator_name НЕ является признаком существования - это просто
        # оператор, которому принадлежит диапазон номера, он заполнен всегда,
        # даже для несуществующих номеров.
        err_code = result['err']
        if status_code == '1' and err_code == '0':
            result['status'] = 'Абонент найден'
        elif status_code == '20' and err_code == '6':
            result['status'] = 'Абонент не найден'
        elif status_code == '0':
            result['status'] = 'В обработке'
        else:
            result['status'] = f'Неизвестная комбинация: status={status_code}, err={err_code} (см. raw_status)'

        result['code'] = status_code
        return result

    def check_phone_ping(self, phone: str, max_wait: int = 20, poll_interval: int = 3) -> Dict:
        """
        Отправляет Ping-SMS — специальное сообщение, невидимое на телефоне,
        которое используется именно для проверки доступности абонента
        В РЕАЛЬНОМ ВРЕМЕНИ (в отличие от HLR, где данные у оператора могут
        быть закэшированы и устаревшими на сутки и более).

        Параметр mes не используется (согласно документации SMSC для ping=1).
        Статус доставки опрашивается несколько раз, пока не придёт финальный
        результат или не истечёт max_wait секунд.

        ВАЖНО: этот метод НЕ делает автоматических повторных попыток при
        ошибке флуд-защиты (код 9) - Ping платный, и повторная отправка без
        ведома пользователя означает повторное списание денег. Решение о
        повторной проверке должно приниматься явно на уровне интерфейса
        (см. cooldown-проверку в app.py), а не молча внутри этого метода.
        """
        phone = re.sub(r'\D', '', phone)

        params = {
            **self._auth_params(),
            "fmt": 1,
            "charset": "utf-8",
            "phones": phone,
            "translit": 0,
            "id": 0,
            "ping": 1,
        }

        try:
            response = requests.get(f"{self.base_url}send.php", params=params, timeout=15)
            raw = response.text.strip()
            self._log_raw("send.php (ping)", raw)

            parts = raw.split(",")

            if len(parts) >= 2 and parts[1].startswith("-"):
                return {
                    'phone': phone,
                    'ping_status': f'Ошибка отправки ping-sms: {describe_error(parts[1])}',
                    'ping_code': parts[1],
                    'raw_ping_send': raw,
                }

            if len(parts) < 2:
                return {
                    'phone': phone,
                    'ping_status': 'Ошибка',
                    'ping_code': f'Неожиданный формат ответа: {raw!r}',
                    'raw_ping_send': raw,
                }

            msg_id = parts[0]

            # Опрашиваем статус несколько раз - доставка ping-sms может
            # занимать больше времени, чем HLR-ответ.
            elapsed = 0
            status_raw = None
            while elapsed < max_wait:
                time.sleep(poll_interval)
                elapsed += poll_interval

                status_params = {
                    **self._auth_params(),
                    "fmt": 1,
                    "charset": "utf-8",
                    "phone": phone,
                    "id": msg_id,
                    "all": 0,
                }
                status_response = requests.get(f"{self.base_url}status.php", params=status_params, timeout=15)
                status_raw = status_response.text.strip()
                self._log_raw(f"status.php (ping, +{elapsed}s)", status_raw)

                parts_status = status_raw.split(",")
                # Если статус ещё "0" (в очереди/передано оператору) - ждём ещё,
                # иначе (доставлено/ошибка/не доставлено) - можно завершать раньше срока.
                if len(parts_status) >= 1 and parts_status[0] not in ("0", ""):
                    break

            return self._map_ping_status(status_raw, phone, msg_id)

        except requests.RequestException as e:
            return {
                'phone': phone,
                'ping_status': 'Исключение',
                'ping_code': str(e),
            }

    def _map_ping_status(self, status_raw: str, phone: str, msg_id: str) -> Dict:
        """
        Разбирает ответ status.php для Ping-SMS.

        Формат для обычных SMS/ping (fmt=1, all=0):
        <статус>,<время>,<код ошибки sms>

        ВАЖНО: точные значения статуса для ping-sms я не нашёл в официально
        подтверждённой документации (аналогично истории с HLR-кодами) -
        ниже общепринятая трактовка, но её стоит откалибровать по вашим
        реальным данным через raw_ping_status, как мы делали для HLR.
        Общий смысл: status=1 -> сообщение доставлено -> телефон СЕЙЧАС в сети;
        отрицательный/ошибочный статус после истечения времени ожидания ->
        телефон недоступен/выключен.
        """
        parts = status_raw.split(",") if status_raw else []

        if len(parts) == 2 and parts[1].startswith("-"):
            return {
                'phone': phone,
                'ping_status': f'Ошибка получения статуса ping: {describe_error(parts[1])}',
                'ping_code': parts[1],
                'raw_ping_status': status_raw,
            }

        def get(i):
            return parts[i] if len(parts) > i else None

        status_code = get(0)
        result = {
            'phone': phone,
            'ping_id': msg_id,
            'ping_status_code': status_code,
            'ping_time': get(1),
            'ping_err': get(2),
            'raw_ping_status': status_raw,
        }

        if status_code == '1':
            result['ping_status'] = 'Онлайн (ping доставлен)'
        elif status_code == '0':
            result['ping_status'] = 'Нет ответа за отведённое время (возможно, оффлайн)'
        elif status_code in ('3', '20'):
            result['ping_status'] = 'Оффлайн / недоступен'
        else:
            result['ping_status'] = f'Неизвестный статус ping: {status_code} (см. raw_ping_status)'

        result['ping_code'] = status_code
        return result

    def check_phone_full(self, phone: str) -> Dict:
        """Совмещает HLR-проверку (существует ли номер) и Ping (в сети ли он сейчас)."""
        hlr_result = self.check_phone_hlr(phone)
        ping_result = self.check_phone_ping(phone)
        # ping_result уже содержит 'phone' - не дублируем
        ping_result.pop('phone', None)
        return {**hlr_result, **ping_result}

    def check_multiple_phones(self, phones: List[str], with_ping: bool = False) -> List[Dict]:
        results = []
        for phone in phones:
            if with_ping:
                result = self.check_phone_full(phone)
            else:
                result = self.check_phone_hlr(phone)
            results.append(result)
            time.sleep(1)
        return results

    def get_balance(self) -> Dict:
        """Возвращает текущий баланс аккаунта SMSC."""
        params = {
            **self._auth_params(),
            "fmt": 1,
            "cur": 1,
        }
        try:
            response = requests.get(f"{self.base_url}balance.php", params=params, timeout=15)
            raw = response.text.strip()
            self._log_raw("balance.php", raw)

            parts = raw.split(",")
            if len(parts) >= 2 and parts[1].startswith("-"):
                return {'status': 'Ошибка', 'code': parts[1], 'raw': raw}

            return {'status': 'OK', 'balance': parts[0], 'raw': raw}
        except requests.RequestException as e:
            return {'status': 'Исключение', 'code': str(e)}
