import streamlit as st
import pandas as pd
import re
import time
from dotenv import load_dotenv
import os
from utils.smsc_api import SMSC_API
from utils.file_processor import FileProcessor

# Загружаем переменные окружения
load_dotenv()

# Ping-SMS платный и дорогой, а SMSC также сама блокирует слишком частые
# повторные запросы по одному номеру (см. код ошибки 9). Поэтому приложение
# само отслеживает, когда номер последний раз пинговался, и предупреждает
# ДО отправки, а не молча повторяет платный запрос.
PING_COOLDOWN_SECONDS = 90  # с запасом сверх ограничения SMSC (~60 сек)

if 'ping_last_checked' not in st.session_state:
    st.session_state.ping_last_checked = {}  # phone -> unix timestamp последней Ping-проверки


def get_ping_cooldown_remaining(phone: str) -> int:
    """Сколько секунд осталось ждать перед повторным Ping этого номера. 0 - можно проверять."""
    last = st.session_state.ping_last_checked.get(phone)
    if last is None:
        return 0
    remaining = int(PING_COOLDOWN_SECONDS - (time.time() - last))
    return max(0, remaining)


def mark_ping_checked(phone: str):
    st.session_state.ping_last_checked[phone] = time.time()


def run_checks_with_cooldown(smsc_client, phones, do_hlr: bool, do_ping_requested: bool, force_all: bool):
    """
    Проверяет список номеров в выбранном режиме (HLR / Ping / оба).
    Ping выполняется только если он запрошен И (номер не в cooldown ИЛИ
    пользователь явно подтвердил принудительную проверку всех номеров).
    """
    results = []
    for phone in phones:
        phone_clean = re.sub(r'\D', '', phone)
        do_ping = False
        skipped_due_to_cooldown = False
        if do_ping_requested:
            if force_all or get_ping_cooldown_remaining(phone_clean) == 0:
                do_ping = True
            else:
                skipped_due_to_cooldown = True

        if do_hlr and do_ping:
            result = smsc_client.check_phone_full(phone)
            mark_ping_checked(phone_clean)
        elif do_hlr and not do_ping:
            result = smsc_client.check_phone_hlr(phone)
            if skipped_due_to_cooldown:
                result['ping_status'] = f'Пропущено (Ping проверялся менее {PING_COOLDOWN_SECONDS} сек назад)'
        elif not do_hlr and do_ping:
            result = smsc_client.check_phone_ping(phone)
            mark_ping_checked(phone_clean)
        else:
            # только Ping запрошен, но пропущен из-за cooldown - HLR не запрошен вовсе
            result = {'phone': phone, 'ping_status': f'Пропущено (Ping проверялся менее {PING_COOLDOWN_SECONDS} сек назад)'}

        results.append(result)
        time.sleep(1)
    return results

# Отладочный вывод сырых ответов SMSC отключён для пользователей приложения
# (это техническая функция для разработчика, менеджеру она не нужна).
DEBUG_MODE = False

def get_smsc_credentials():
    """
    Получает логин/API-ключ SMSC из Secrets Streamlit Cloud (приоритет) или
    из переменных окружения (.env, для локального запуска администратором).
    Сотрудники, открывающие приложение, эти значения не видят и не вводят -
    ключ настраивается один раз администратором на сервере.
    """
    login, api_key = "", ""
    try:
        login = st.secrets.get("SMSC_LOGIN", "")
        api_key = st.secrets.get("SMSC_API_KEY", "")
    except Exception:
        pass  # secrets.toml не настроен - это нормально для локального запуска

    if not login and not api_key:
        login = os.getenv("SMSC_LOGIN", "")
        api_key = os.getenv("SMSC_API_KEY", "")

    return login, api_key


# Заголовок
st.set_page_config(
    page_title="Проверка номеров: HLR + Ping",
    page_icon="📱",
    layout="wide"
)
st.title("📱 Проверка номеров: существование (HLR) и активность (Ping)")

st.info(
    "**HLR-проверка** — узнаёт, существует ли номер у оператора (зарегистрирована ли на него SIM-карта). "
    "Стоимость: **1,60 руб.** за проверку, одинаково для всех операторов.\n\n"
    "**Ping-проверка** — узнаёт, включён ли телефон и на связи ли он **прямо сейчас** (в реальном времени, "
    "в отличие от HLR, где данные у оператора могут быть закэшированы). "
    "Стоимость: **от ~6,50 до ~12 руб.** за проверку — зависит от оператора получателя (у разных "
    "операторов разная цена доставки)."
)

st.markdown("---")

login, api_key = get_smsc_credentials()
credentials_configured = bool(login and api_key)

# Боковая панель с настройками
with st.sidebar:
    st.header("⚙️ Настройки")

    if credentials_configured:
        st.success("🔒 Доступ к SMSC настроен")
    else:
        # Ключ не настроен администратором на сервере - показываем поле ввода
        # только как запасной вариант (например, для локальной проверки).
        st.warning("⚠️ API-ключ не настроен администратором")
        with st.expander("Указать логин/ключ вручную (временно, для теста)"):
            login = st.text_input("Логин SMSC", value=login, type="password")
            api_key = st.text_input("API-ключ", value=api_key, type="password")

    with_ping = st.radio(
        "🔍 Какую проверку выполнять",
        ["Только HLR", "Только Ping", "HLR + Ping"],
        index=0,
        help="HLR — существует ли номер у оператора (1,60 руб). "
             "Ping — в сети ли телефон прямо сейчас (от ~6,50 до ~12 руб). "
             f"Повторная Ping-проверка одного номера чаще, чем раз в {PING_COOLDOWN_SECONDS} сек, "
             "потребует отдельного подтверждения."
    )
    do_hlr = with_ping in ("Только HLR", "HLR + Ping")
    do_ping_requested = with_ping in ("Только Ping", "HLR + Ping")

    st.markdown("---")

    if login and api_key and st.button("💰 Проверить баланс"):
        balance_info = SMSC_API(login, api_key, debug=DEBUG_MODE).get_balance()
        if balance_info.get('status') == 'OK':
            st.success(f"Баланс: {balance_info.get('balance')} руб.")
        else:
            st.error(f"Не удалось получить баланс: {balance_info.get('code')}")

# Основная область
tab1, tab2, tab3 = st.tabs(["🔍 Один номер", "📋 Несколько номеров", "📂 Загрузить файл"])

# Инициализируем API
if login and api_key:
    smsc = SMSC_API(login, api_key, debug=DEBUG_MODE)
else:
    st.warning("⚠️ Введите логин и API-ключ в боковой панели")
    smsc = None

# --- TAB 1: Один номер ---
with tab1:
    st.subheader("Проверка одного номера")

    phone_input = st.text_input("Введите номер телефона", placeholder="79001234567", help="Вводите без + и пробелов")

    phone_clean_preview = re.sub(r'\D', '', phone_input) if phone_input else ""
    force_ping_single = True
    if do_ping_requested and phone_clean_preview:
        remaining = get_ping_cooldown_remaining(phone_clean_preview)
        if remaining > 0:
            st.warning(
                f"⏳ Этот номер уже проверялся на Ping менее {PING_COOLDOWN_SECONDS} сек назад "
                f"(осталось ждать ~{remaining} сек). SMSC может отклонить повторный запрос "
                f"с ошибкой (код 9), но деньги за отправку всё равно спишутся."
            )
            force_ping_single = st.checkbox(
                "Всё равно отправить Ping сейчас (спишутся деньги)",
                key=f"force_ping_{phone_clean_preview}"
            )

    if st.button("Проверить", key="check_single", type="primary"):
        if not smsc:
            st.error("❌ Введите API-ключи в боковой панели")
        elif not phone_input:
            st.error("❌ Введите номер телефона")
        else:
            do_ping = do_ping_requested and force_ping_single
            if do_hlr and do_ping:
                spinner_text = "Отправляем HLR + Ping-SMS (может занять до ~25 сек)..."
            elif do_ping:
                spinner_text = "Отправляем Ping-SMS (может занять до ~20 сек)..."
            else:
                spinner_text = "Отправляем HLR-запрос..."

            with st.spinner(spinner_text):
                if do_hlr and do_ping:
                    result = smsc.check_phone_full(phone_input)
                    mark_ping_checked(phone_clean_preview)
                elif do_ping:
                    result = smsc.check_phone_ping(phone_input)
                    mark_ping_checked(phone_clean_preview)
                else:
                    result = smsc.check_phone_hlr(phone_input)

                if do_ping_requested and not do_ping:
                    st.info("ℹ️ Ping пропущен из-за недавней проверки этого номера.")

                # Сохраняем в историю
                if 'results' not in st.session_state or st.session_state.results is None:
                    st.session_state.results = pd.DataFrame([result])
                else:
                    st.session_state.results = pd.concat([st.session_state.results, pd.DataFrame([result])], ignore_index=True)

                # Показываем результат
                n_cols = 1 + (2 if do_hlr else 0) + (1 if do_ping else 0)
                cols = st.columns(max(n_cols, 2))
                col_i = 0
                cols[col_i].metric("📞 Номер", result.get('phone', phone_input))
                col_i += 1

                if do_hlr:
                    status_val = result.get('status', '—')
                    if status_val == 'Абонент найден':
                        cols[col_i].metric("✅ HLR", status_val)
                    elif status_val == 'Абонент не найден':
                        cols[col_i].metric("❌ HLR", status_val)
                    else:
                        cols[col_i].metric("❓ HLR", status_val)
                    col_i += 1
                    cols[col_i].metric("🔢 Код", result.get('code'))
                    col_i += 1

                if do_ping:
                    ping_val = result.get('ping_status', '—')
                    if ping_val == 'Онлайн (ping доставлен)':
                        cols[col_i].metric("📶 Ping", ping_val)
                    else:
                        cols[col_i].metric("📴 Ping", ping_val)

# --- TAB 2: Несколько номеров ---
with tab2:
    st.subheader("Проверка нескольких номеров")

    phones_text = st.text_area(
        "Введите номера (каждый с новой строки)",
        placeholder="79001234567\n79009876543\n79161112233",
        height=150
    )

    force_ping_multi = True
    if do_ping_requested and phones_text.strip():
        phones_preview = [re.sub(r'\D', '', p.strip()) for p in phones_text.split('\n') if p.strip()]
        in_cooldown = [p for p in phones_preview if get_ping_cooldown_remaining(p) > 0]
        if in_cooldown:
            st.warning(
                f"⏳ {len(in_cooldown)} из {len(phones_preview)} номеров проверялись на Ping менее "
                f"{PING_COOLDOWN_SECONDS} сек назад. По умолчанию для них Ping будет пропущен."
            )
            force_ping_multi = st.checkbox(
                "Всё равно выполнить Ping для ВСЕХ номеров, включая недавно проверенные (спишутся деньги за каждый)",
                key="force_ping_multi"
            )

    if st.button("Проверить все", key="check_multiple", type="primary"):
        if not smsc:
            st.error("❌ Введите API-ключи в боковой панели")
        elif not phones_text:
            st.error("❌ Введите номера")
        else:
            phones = [p.strip() for p in phones_text.split('\n') if p.strip()]
            if do_hlr and do_ping_requested:
                spinner_text = f"Проверяем {len(phones)} номеров (HLR + Ping, это дольше)..."
            elif do_ping_requested:
                spinner_text = f"Проверяем {len(phones)} номеров (Ping)..."
            else:
                spinner_text = f"Проверяем {len(phones)} номеров (HLR)..."
            with st.spinner(spinner_text):
                results = run_checks_with_cooldown(smsc, phones, do_hlr, do_ping_requested, force_ping_multi)
                df_results = pd.DataFrame(results)

                # Сохраняем в историю
                if 'results' not in st.session_state or st.session_state.results is None:
                    st.session_state.results = df_results
                else:
                    st.session_state.results = pd.concat([st.session_state.results, df_results], ignore_index=True)

                st.success(f"✅ Проверено {len(results)} номеров")

                employee_df = FileProcessor.to_employee_view(df_results)
                st.dataframe(FileProcessor.style_employee_view(employee_df), use_container_width=True)

                with st.expander("🔧 Технические детали (все поля)"):
                    st.dataframe(df_results, use_container_width=True)

                # Кнопки скачать результат
                col_dl1, col_dl2 = st.columns(2)
                col_dl1.download_button(
                    label="📥 Скачать для сотрудников (CSV)",
                    data=employee_df.to_csv(index=False),
                    file_name="hlr_results_simple.csv",
                    mime="text/csv"
                )
                col_dl2.download_button(
                    label="📥 Скачать полные данные (CSV)",
                    data=df_results.to_csv(index=False),
                    file_name="hlr_results_full.csv",
                    mime="text/csv"
                )
with tab3:
    st.subheader("Загрузка Excel-файла")

    uploaded_file = st.file_uploader(
        "Выберите Excel-файл",
        type=['xlsx', 'xls'],
        help="Файл должен содержать столбец с номерами телефонов"
    )

    if uploaded_file is not None:
        try:
            # Показываем превью файла
            df_preview = pd.read_excel(uploaded_file, engine='openpyxl', nrows=5)
            st.write("Превью файла:")
            st.dataframe(df_preview, use_container_width=True)

            # Извлекаем номера
            processor = FileProcessor()
            phones = processor.extract_phones_from_excel(uploaded_file)

            st.info(f"Найдено {len(phones)} номеров")

            force_ping_file = True
            if do_ping_requested and phones:
                phones_clean = [re.sub(r'\D', '', p) for p in phones]
                in_cooldown = [p for p in phones_clean if get_ping_cooldown_remaining(p) > 0]
                if in_cooldown:
                    st.warning(
                        f"⏳ {len(in_cooldown)} из {len(phones)} номеров проверялись на Ping менее "
                        f"{PING_COOLDOWN_SECONDS} сек назад. По умолчанию для них Ping будет пропущен."
                    )
                    force_ping_file = st.checkbox(
                        "Всё равно выполнить Ping для ВСЕХ номеров, включая недавно проверенные (спишутся деньги за каждый)",
                        key="force_ping_file"
                    )

            if st.button("Начать проверку", key="check_file", type="primary"):
                if not smsc:
                    st.error("❌ Введите API-ключи в боковой панели")
                elif not phones:
                    st.error("❌ Не найдены номера в файле")
                else:
                    if do_hlr and do_ping_requested:
                        spinner_text = f"Проверяем {len(phones)} номеров (HLR + Ping, это дольше)..."
                    elif do_ping_requested:
                        spinner_text = f"Проверяем {len(phones)} номеров (Ping)..."
                    else:
                        spinner_text = f"Проверяем {len(phones)} номеров (HLR)..."
                    with st.spinner(spinner_text):
                        results = run_checks_with_cooldown(smsc, phones, do_hlr, do_ping_requested, force_ping_file)
                        df_results = pd.DataFrame(results)

                        # Сохраняем в историю
                        if 'results' not in st.session_state or st.session_state.results is None:
                            st.session_state.results = df_results
                        else:
                            st.session_state.results = pd.concat([st.session_state.results, df_results], ignore_index=True)

                        st.success(f"✅ Проверено {len(results)} номеров")

                        employee_df = FileProcessor.to_employee_view(df_results)
                        st.dataframe(FileProcessor.style_employee_view(employee_df), use_container_width=True)

                        with st.expander("🔧 Технические детали (все поля)"):
                            st.dataframe(df_results, use_container_width=True)

                        # Кнопки скачать результат (CSV)
                        col_dl1, col_dl2 = st.columns(2)
                        col_dl1.download_button(
                            label="📥 Для сотрудников (CSV)",
                            data=employee_df.to_csv(index=False),
                            file_name="hlr_results_simple.csv",
                            mime="text/csv"
                        )
                        col_dl2.download_button(
                            label="📥 Полные данные (CSV)",
                            data=df_results.to_csv(index=False),
                            file_name="hlr_results_full.csv",
                            mime="text/csv"
                        )

                        # Экспорт в Excel в памяти (упрощённая версия - для сотрудников)
                        from io import BytesIO
                        excel_buffer = BytesIO()
                        with pd.ExcelWriter(excel_buffer, engine='openpyxl') as writer:
                            employee_df.to_excel(writer, index=False)
                        st.download_button(
                            label="📥 Скачать для сотрудников (Excel)",
                            data=excel_buffer.getvalue(),
                            file_name="hlr_results.xlsx",
                            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                        )
        except Exception as e:
            st.error(f"Ошибка при чтении файла: {e}")

# --- История проверок ---
if 'results' in st.session_state and st.session_state.results is not None and len(st.session_state.results) > 0:
    st.markdown("---")
    st.subheader("📜 История проверок")

    history_employee_df = FileProcessor.to_employee_view(st.session_state.results)

    # Кнопка очистки истории
    col1, col2 = st.columns([3, 1])
    with col1:
        st.dataframe(FileProcessor.style_employee_view(history_employee_df), use_container_width=True)
    with col2:
        if st.button("🗑️ Очистить историю"):
            st.session_state.results = pd.DataFrame()
            st.rerun()

    with st.expander("🔧 Технические детали всей истории"):
        st.dataframe(st.session_state.results, use_container_width=True)

    # Экспорт всей истории
    col_h1, col_h2 = st.columns(2)
    col_h1.download_button(
        label="📥 История для сотрудников (CSV)",
        data=history_employee_df.to_csv(index=False),
        file_name="all_hlr_history_simple.csv",
        mime="text/csv"
    )
    col_h2.download_button(
        label="📥 История полная (CSV)",
        data=st.session_state.results.to_csv(index=False),
        file_name="all_hlr_history_full.csv",
        mime="text/csv"
    )

# --- Подвал ---
st.markdown("---")
st.caption("Приложение использует HLR-запросы через SMSC API. Каждый запрос тарифицируется.")
