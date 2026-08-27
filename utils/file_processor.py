import pandas as pd
import re
from datetime import datetime
from typing import List, Dict

class FileProcessor:
    @staticmethod
    def extract_phones_from_excel(file) -> List[str]:
        """Извлекает номера телефонов из Excel-файла"""
        df = pd.read_excel(file, engine='openpyxl')

        # Ищем столбец с номерами (по названию или по содержимому)
        phone_columns = ['phone', 'phone number', 'номер', 'телефон', 'мобильный', 'phone_number']
        phone_col = None

        for col in df.columns:
            col_lower = col.lower().strip()
            if any(keyword in col_lower for keyword in phone_columns):
                phone_col = col
                break

        if phone_col is None:
            # Если не нашли по названию, пробуем найти по содержимому
            for col in df.columns:
                sample = str(df[col].iloc[0]) if len(df) > 0 else ''
                if re.search(r'\d{10,15}', sample):
                    phone_col = col
                    break

        if phone_col is None:
            # Если всё равно не нашли, берём первую текстовую колонку
            for col in df.columns:
                if df[col].dtype == object:
                    phone_col = col
                    break

        if phone_col is None:
            return []

        # Извлекаем и очищаем номера
        phones = df[phone_col].dropna().astype(str).tolist()
        cleaned_phones = []
        for phone in phones:
            # Оставляем только цифры
            digits = re.sub(r'\D', '', phone)
            if len(digits) >= 10 and len(digits) <= 15:
                # Убираем ведущий 8, оставляем 7 для международного формата
                if digits.startswith('8') and len(digits) == 11:
                    digits = '7' + digits[1:]
                elif len(digits) == 10:
                    # 10-значный номер без кода страны (обычно начинается с 9)
                    digits = '7' + digits
                cleaned_phones.append(digits)

        return cleaned_phones

    @staticmethod
    def format_results_to_df(results: List[Dict]) -> pd.DataFrame:
        """Форматирует результаты в DataFrame для отображения"""
        return pd.DataFrame(results)

    @staticmethod
    def to_employee_view(df: pd.DataFrame) -> pd.DataFrame:
        """
        Упрощённое представление результатов для сотрудника: только то,
        что нужно для принятия решения - существует ли номер и в сети ли он.
        Технические поля (id, imsi, коды, сырые ответы) скрываются.

        Добавляет колонку "Активен" (Да/Нет/Неизвестно), вычисленную по тем
        проверкам, которые реально выполнялись для этой строки: если делали
        только HLR - смотрим только на существование; если только Ping -
        только на онлайн; если оба - строка "Активен: Да" только если ОБЕ
        проверки положительные.
        """
        if df is None or len(df) == 0:
            return pd.DataFrame()

        out = pd.DataFrame()
        out['Номер'] = df.get('phone', '')

        has_hlr = 'status' in df.columns
        has_ping = 'ping_status' in df.columns

        if has_hlr:
            out['Существует'] = df['status'].map({
                'Абонент найден': 'Да',
                'Абонент не найден': 'Нет',
            }).fillna(df['status'])  # если что-то нестандартное - показываем как есть

        if 'operator_name' in df.columns:
            out['Оператор'] = df['operator_name']

        if 'country_name' in df.columns:
            out['Страна'] = df['country_name']

        if has_ping:
            def _ping_human(v):
                if v == 'Онлайн (ping доставлен)':
                    return 'Да'
                if v in ('Оффлайн / недоступен', 'Нет ответа за отведённое время (возможно, оффлайн)'):
                    return 'Нет'
                if pd.isna(v):
                    return 'Не проверялось'
                return v  # ошибки/неизвестные статусы показываем как есть, чтобы не терять сигнал
            out['В сети сейчас'] = df['ping_status'].map(_ping_human)

        if 'time' in df.columns:
            def _fmt_time(v):
                try:
                    return datetime.fromtimestamp(int(v)).strftime('%d.%m.%Y %H:%M:%S')
                except (ValueError, TypeError):
                    return ''
            out['Время проверки'] = df['time'].map(_fmt_time)

        def _active_flag(row_idx):
            checks = []
            if has_hlr:
                status_val = df.at[row_idx, 'status']
                if status_val == 'Абонент найден':
                    checks.append(True)
                elif status_val == 'Абонент не найден':
                    checks.append(False)
                # любой другой статус (ошибка) - в подсчёт не берём, недостаточно данных
            if has_ping:
                ping_val = df.at[row_idx, 'ping_status']
                if ping_val == 'Онлайн (ping доставлен)':
                    checks.append(True)
                elif ping_val in ('Оффлайн / недоступен', 'Нет ответа за отведённое время (возможно, оффлайн)'):
                    checks.append(False)
            if not checks:
                return 'Неизвестно'
            return 'Да' if all(checks) else 'Нет'

        out['Активен'] = [ _active_flag(i) for i in df.index ]

        return out

    @staticmethod
    def style_employee_view(df: pd.DataFrame):
        """
        Возвращает pandas Styler с подсветкой строк по колонке "Активен":
        светло-зелёный для активных номеров, светло-розовый для неактивных.
        Строки с неизвестным статусом (ошибки/недостаточно данных) не подсвечиваются.
        """
        if df is None or len(df) == 0 or 'Активен' not in df.columns:
            return df

        def highlight(row):
            val = row.get('Активен')
            if val == 'Да':
                return ['background-color: #d9f5d9'] * len(row)
            elif val == 'Нет':
                return ['background-color: #fbdce6'] * len(row)
            return [''] * len(row)

        return df.style.apply(highlight, axis=1)
