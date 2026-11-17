"""Верити — голосовой ассистент для Windows.
Запуск: run.bat (или pythonw main.py)."""
import json
import logging
import os
import queue
import random
import difflib
import re
import sys
import tempfile
import threading
import time

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "config.json")

# журнал — до всех библиотек: если какой-то не хватает, Верити падает без окна, и узнать причину можно только тут
# не больше ~1 МБ: старая часть уходит в verity.log.1, ещё более старая удаляется — диск не забивается
from logging.handlers import RotatingFileHandler  # noqa: E402
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    handlers=[RotatingFileHandler(os.path.join(BASE, "verity.log"), maxBytes=1_000_000,
                                                  backupCount=1, encoding="utf-8")])
log = logging.getLogger("verity")
log.info("Python %s", sys.version.split()[0])


def fatal(text):
    """Верити запускается без окна консоли, поэтому о том, что не так, говорим окном Windows."""
    log.error(text)
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, "Верити не запустилась", 0x10)
    except Exception:
        pass
    sys.exit(1)


try:
    from PySide6.QtCore import QObject, Qt, Signal
    from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPen, QPixmap
    from PySide6.QtWidgets import QApplication, QInputDialog, QMenu, QSystemTrayIcon

    from actions import VERITY_URL, Actions, Reply
    from ai import AI
    from local_ai import LocalAI
    from persona import LINES, gendered, line, set_user
    from avatar import Avatar
    import skins
    import zapret
    from voice import (Listener, Speaker, default_input_name, google_stt, input_devices, make_glitch_sound,
                       merge_stt, play_sound_async)
except ImportError as e:
    log.exception("Не хватает библиотек")
    fatal(f"Не установлены нужные библиотеки ({e.name or e}).\n\n"
          f"Запусти setup.bat в папке Верити и дождись надписи «Готово», потом снова run.bat.\n"
          f"Если setup.bat выдаёт ошибку — сфотографируй его окно.\n\nPython {sys.version.split()[0]}")

DEFAULTS = {
    "enable_ai": False,
    "local_ai": True,
    "local_model": "qwen3:8b",
    "barge_in": True,
    "mic_device": None,
    "chatter": True,
    "chatter_min_minutes": 15,
    "chatter_max_minutes": 40,
    "chat_button": False,      # кнопка чата в правом нижнем углу экрана (включается в «Настройках»)
    "pranks": False,           # приколы: сама пишет записку в Блокнот и открывает (включить: true)
    "pranks_min_minutes": 30,
    "pranks_max_minutes": 90,
    "roam_chance": 0.5,
    "teleport_on_answer": True,
    "skin": "",              # папка скина в skins/; "" — стандартная Верити (меню в трее → «Настройки…» → «Внешность»)
    "search_when_unsure": True,
    "ai_context": 4096,      # сколько текста нейросеть держит в памяти; больше — нужно больше памяти
    "voice_volume": 100,     # громкость голоса Верити 0..100 (общая громкость Windows не меняется)
    "mic_gain": 1.0,         # громкость микрофона для Верити: 1 — как есть, 2 — вдвое громче
    "mic_sensitivity": 100,  # 100 — слышит всё; меньше — тихий шум и фон отбрасываются
    "user_name_mode": "none",  # как называть пользователя: "custom" — user_name, "windows" — имя пользователя
                               # Windows, "pc" — имя компьютера, "none" — не называть
    "user_name": "",
    "user_gender": "male",     # "male" / "female" — в каком роде Верити к тебе обращается
    "browser": "",
    "replies": {},           # свои ответы: {"фраза": "что ответит Верити"} — проверяются раньше команд и нейросети  # в каком браузере открывать сайты: "хром", "яндекс", "опера"… или путь к exe; "" — как в Windows
    "anthropic_api_key": "",
    "model": "claude-opus-5-5",
    "wake_words": ["верити", "verity", "вери", "вэрити", "вирити", "берити", "ферити", "вериди"],
    "use_google_stt": True,
    "voice": "ru-RU-SvetlanaNeural",
    "tts_engine": "blip",     # blip — звук речи в стиле Undertale (бипы вместо голоса)
    "blip_pitch": 1.0,       # высота бипов: 1.0 — обычная, 1.3 — писклявее, 0.8 — ниже
    "voice_rate": "+10%",
    "voice_pitch": "+15Hz",
    "voice_shift": 0,
    "voice_lively": False,
    "voice_fx": 1.8,
    "listen_window_seconds": 8,
    "followup_seconds": 6,
    "hide_after_seconds": 12,
    "greet_on_start": True,
    "vosk_model": "models/vosk-model-small-ru-0.22",
    "sites": {
        "ютуб": "https://www.youtube.com", "youtube": "https://www.youtube.com",
        "гугл": "https://www.google.com", "google": "https://www.google.com",
        "яндекс": "https://ya.ru", "вк": "https://vk.com", "вконтакте": "https://vk.com",
        "почту": "https://mail.google.com", "почта": "https://mail.google.com",
        "гитхаб": "https://github.com", "твич": "https://www.twitch.tv",
        "википедию": "https://ru.wikipedia.org", "википедия": "https://ru.wikipedia.org",
        "переводчик": "https://translate.google.com", "карты": "https://yandex.ru/maps",
        "погоду": "https://yandex.ru/pogoda", "chatgpt": "https://chatgpt.com",
        "клод": "https://claude.ai", "кинопоиск": "https://www.kinopoisk.ru",
        "авито": "https://www.avito.ru", "озон": "https://www.ozon.ru",
        "вайлдберриз": "https://www.wildberries.ru"
    },
    "apps": {
        "блокнот": "notepad", "калькулятор": "calc", "проводник": "explorer",
        "диспетчер задач": "taskmgr", "параметры": "ms-settings:", "настройки": "ms-settings:",
        "настройки windows": "ms-settings:", "настройки виндовс": "ms-settings:", "параметры windows": "ms-settings:",
        "настройки компьютера": "ms-settings:",
        "панель управления": "control", "командную строку": "cmd", "командная строка": "cmd",
        "терминал": "wt", "пейнт": "mspaint", "paint": "mspaint", "ворд": "winword",
        "эксель": "excel", "браузер": "https://www.google.com",
        "настройки звука": "ms-settings:sound", "настройки экрана": "ms-settings:display",
        "корзину": "shell:RecycleBinFolder", "загрузки": "shell:Downloads"
    },
    "app_aliases": {
        "хром": "chrome", "гугл хром": "google chrome", "стим": "steam", "дискорд": "discord",
        "телеграм": "telegram", "телеграмм": "telegram", "спотифай": "spotify",
        "опера": "opera", "яндекс браузер": "yandex", "вс код": "visual studio code",
        "визуал студио код": "visual studio code", "обс": "obs", "эпик геймс": "epic games",
        "ватсап": "whatsapp", "зум": "zoom", "фотошоп": "photoshop", "майнкрафт": "minecraft"
    }
}


def _config_blank():
    """config.json нет или он пустой (всё стёрли) — такой можно спокойно создать заново."""
    try:
        with open(CONFIG_PATH, encoding="utf-8-sig") as f:
            return not f.read().strip()
    except FileNotFoundError:
        return True


def _edit_config(change):
    """Прочитать config.json, применить change(data) и записать. Файл с ошибкой не перезаписываем —
    иначе пропадут правки пользователя. Возвращает текст ошибки или None."""
    try:
        if _config_blank():
            data = {}
        else:
            with open(CONFIG_PATH, encoding="utf-8-sig") as f:  # Блокнот иногда сохраняет с BOM
                data = json.load(f)
    except Exception:
        log.exception("config.json с ошибкой, не записываю")
        return "в config.json ошибка. Исправь её или удали файл, он создастся заново."
    change(data)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return None


def save_setting(key, value):
    """Записать одну настройку в config.json, не трогая остальные."""
    _edit_config(lambda data: data.__setitem__(key, value))


def save_entry(section, name, value):
    """Свой сайт или программа: section "sites"/"apps". value=None — убрать; встроенные записи
    убираются пометкой null, иначе вернулись бы из настроек по умолчанию."""
    def change(data):
        entries = data.setdefault(section, {})
        if value is None and name not in DEFAULTS.get(section, {}):
            entries.pop(name, None)
        else:
            entries[name] = value
    return _edit_config(change)


def user_display_name(cfg):
    """Как Верити называет пользователя — по настройке user_name_mode."""
    mode = cfg.get("user_name_mode", "none")
    if mode == "custom":
        return cfg.get("user_name", "").strip()
    if mode == "windows":
        return os.environ.get("USERNAME", "")
    if mode == "pc":
        return os.environ.get("COMPUTERNAME", "")
    return ""


def apply_user(cfg):
    set_user(user_display_name(cfg), cfg.get("user_gender", "male"))


CONFIG_ERROR = ""  # что сказать вслух, если config.json не прочитался


def load_config():
    global CONFIG_ERROR
    CONFIG_ERROR = ""
    cfg = json.loads(json.dumps(DEFAULTS))
    if not _config_blank():
        try:
            with open(CONFIG_PATH, encoding="utf-8-sig") as f:
                user = json.load(f)
            for k, v in user.items():
                if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                    for name, value in v.items():  # null — убранная встроенная запись
                        if value is None:
                            cfg[k].pop(name, None)
                        else:
                            cfg[k][name] = value
                else:
                    cfg[k] = v
            missing = [k for k in DEFAULTS if k not in user]
            if missing:  # файл неполный (его создал setup_ai.bat или он из старой версии) — дописываем
                _edit_config(lambda data: [data.setdefault(k, DEFAULTS[k]) for k in missing])
                log.info("В config.json добавлены недостающие настройки: %s", ", ".join(missing))
        except json.JSONDecodeError as e:
            log.error("config.json с ошибкой (строка %d, символ %d: %s), использую настройки по умолчанию",
                      e.lineno, e.colno, e.msg)
            CONFIG_ERROR = (f"В config.json ошибка в строке {e.lineno}, я его не прочитала и работаю "
                            "со стандартными настройками. Проверь там кавычки, запятые и двойные слеши в путях.")
        except Exception:
            log.exception("config.json не прочитался, использую настройки по умолчанию")
            CONFIG_ERROR = "Не смогла прочитать config.json, работаю со стандартными настройками."
    else:  # файла нет или его содержимое стёрли — создаём заново со стандартными настройками
        log.info("config.json пустой или удалён, создаю заново")
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(DEFAULTS, f, ensure_ascii=False, indent=2)
    return cfg


STOP_RE = re.compile(r"\b(стоп|хватит|молчи|замолчи|помолчи|достаточно|подожди|остановись|перестань|"
                     r"заткнись|умолкни)\b")


def fullscreen_app_active():
    """Открыта игра или видео на весь экран — тогда не лезем со спонтанными репликами."""
    try:
        import ctypes
        from ctypes import wintypes
        u = ctypes.windll.user32
        hwnd = u.GetForegroundWindow()
        if not hwnd:
            return False
        cls = ctypes.create_unicode_buffer(64)
        u.GetClassNameW(hwnd, cls, 64)
        if cls.value in ("Progman", "WorkerW", "Shell_TrayWnd"):
            return False  # рабочий стол
        r = wintypes.RECT()
        u.GetWindowRect(hwnd, ctypes.byref(r))
        return (r.right - r.left) >= u.GetSystemMetrics(0) and (r.bottom - r.top) >= u.GetSystemMetrics(1)
    except Exception:
        return False


class Verity(QObject):
    summon = Signal()
    vanish = Signal()
    mode = Signal(str, float)
    subtitle = Signal(str)
    quit_app = Signal()
    anim = Signal(str)
    roam = Signal()
    hop = Signal()
    move_to = Signal(str)
    settings = Signal(str)   # открыть окно настроек на вкладке
    chat_button = Signal(bool)  # показать / спрятать кнопку чата
    chat_message = Signal(str, str)  # (кто, текст) — для окна чата: «user» или «verity»

    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        # маленькая модель Vosk не знает слова «Верити» и слышит его по-разному, в том числе двумя словами:
        # эти варианты работают всегда, даже если в config.json их нет
        heard = ["верити", "verity", "вери", "вэрити", "вирити", "берити", "ферити", "вериди", "верете", "верети", "вэрэти", "верите", "вере ти", "вери ти", "вери ты", "вери ди", "вери те", "вэри ти", "вэри ты", "вэри ди", "бери ти", "бери ты", "фери ти", "ве рити", "вер ити", "вэр ити", "вир ити", "верит и"]
        words = sorted(set(cfg["wake_words"]) | set(heard), key=len, reverse=True)
        self.wake_re = re.compile(r"(^|\s)(" + "|".join(map(re.escape, words)) + r")(?=\s|$|[,.!?])",
                                  re.IGNORECASE)
        self.ai = AI(cfg)
        self.local = LocalAI(cfg) if cfg.get("local_ai") else None
        if self.local:
            self.local.on_model_change = self._remember_model
        self.actions = Actions(cfg, BASE, self.ai, save=save_entry)
        self.config_mtime = self._config_mtime()
        self.zapret_checked = False  # предлагали ли уже исправить zapret в этом сеансе
        self.ai_problem_said = None  # о какой проблеме нейросети уже сказали — не повторяем каждый раз
        self.actions.local = self.local
        self.actions.save_setting = save_setting
        self.speaker = Speaker(cfg)
        self.jobs = queue.Queue()
        self.awake_until = 0.0
        self.last_partial_wake = 0.0
        self.speaking = False
        self.interrupted = False
        self.speech_words = set()
        self.speech_end = 0.0
        self.last_activity = time.time()
        self.recent_chatter = []
        self.listener = None
        self.glitch_wav = os.path.join(tempfile.gettempdir(), "verity_glitch.wav")
        try:
            make_glitch_sound(self.glitch_wav, self.speaker.volume)
        except Exception:
            self.glitch_wav = None

    def start(self):
        threading.Thread(target=self._worker, daemon=True).start()
        if self.local:
            threading.Thread(target=self.local.start_server, daemon=True).start()
        model =os.path.join(BASE, self.cfg["vosk_model"])
        if os.path.isdir(model):
            self.listener = Listener(model, self.on_final, self.on_partial, self.on_listener_error,
                                     device_name=self.cfg.get("mic_device") or None,
                                     gain=float(self.cfg.get("mic_gain", 1.0)),
                                     sensitivity=int(self.cfg.get("mic_sensitivity", 100)))
            self.listener.start()
        else:
            self.jobs.put(("say", line("no_model")))
            return
        # вступление: анимация появления из скина + приветствие
        self.jobs.put(("say", line("greet") if self.cfg.get("greet_on_start") else "", "intro"))
        if CONFIG_ERROR:
            self.jobs.put(("say", CONFIG_ERROR))
        threading.Thread(target=self._chatter_loop, daemon=True).start()  # сам проверяет, включено ли
        threading.Thread(target=self._prank_loop, daemon=True).start()
        threading.Thread(target=self._prewarm_voice, daemon=True).start()

    def _prewarm_voice(self):
        """Через полминуты после запуска озвучиваем в фоне постоянные фразы и свои ответы — потом они
        звучат сразу, без ожидания сервера Microsoft."""
        time.sleep(30)
        static = [gendered(t) for variants in LINES.values() for t in variants if "{" not in t]
        static += list(self.cfg.get("replies", {}).values())
        self.speaker.prewarm(static)

    # ----- спонтанные появления -----
    def _chatter_loop(self):
        def next_delay():
            lo, hi = self.cfg["chatter_min_minutes"], self.cfg["chatter_max_minutes"]
            return random.uniform(lo, max(lo, hi)) * 60
        due = time.time() + next_delay()
        while True:
            time.sleep(15)
            now = time.time()
            if not self.cfg.get("chatter"):  # выключено галочкой в трее — ждём, отсчёт начнётся заново
                due = now + next_delay()
                continue
            if now < due:
                continue
            quiet = now - self.last_activity > 120 and self.jobs.empty() and not self.speaking
            if quiet and not fullscreen_app_active():
                self.jobs.put(("chatter",))
                due = now + next_delay()
            else:
                due = now + 60  # занят или в игре/фильме — попробуем позже

    def _prank_loop(self):
        def next_delay():
            lo, hi = self.cfg.get("pranks_min_minutes", 30), self.cfg.get("pranks_max_minutes", 90)
            return random.uniform(lo, max(lo, hi)) * 60
        due = time.time() + next_delay()
        while True:
            time.sleep(15)
            now = time.time()
            if not self.cfg.get("pranks"):
                due = now + next_delay()
                continue
            if now < due:
                continue
            quiet = now - self.last_activity > 120 and self.jobs.empty() and not self.speaking
            if quiet and not fullscreen_app_active():
                self.jobs.put(("prank",))
                due = now + next_delay()
            else:
                due = now + 60

    def chatter_line(self):
        hour = time.localtime().tm_hour
        text = None
        if 0 <= hour < 5 and random.random() < 0.5:
            text = line("chatter_night")
        elif 6 <= hour < 11 and random.random() < 0.3:
            text = line("chatter_morning")
        elif self.local and random.random() < 0.4:
            text = self.local.chatter()
        for _ in range(5):
            if text and text not in self.recent_chatter:
                break
            text = line("chatter")
        self.recent_chatter = (self.recent_chatter + [text])[-6:]
        return text

    # ----- события микрофона (поток слушателя) -----
    # обычные слова, на которые имя похоже, но это не оно
    NOT_NAME = {"верить", "верите", "верит", "веришь", "верят", "вертит", "вертеть", "верить", "версия",
                "вирту", "ветер", "берите", "берётся", "верный", "верно"}

    @staticmethod
    def _norm(w):
        return w.replace("ё", "е").replace("э", "е").replace("ы", "и").replace("й", "и").replace("б", "в").replace("ф", "в").replace("е", "и").replace("а", "и").replace("я", "и").replace("о", "и")

    def fix_name(self, text):
        """«вери ты», «вэрити», «бери ти», «веретти» и другие похожие на «Верити» слова заменяем на «верити»:
        маленькая модель распознавания не знает этого имени и слышит его по-разному."""
        tokens = text.lower().split()
        out, i = [], 0
        target = "вересе"  # заглушка, ниже настоящая цель
        target = self._norm("верити")
        while i < len(tokens):
            hit = 0
            for n in (2, 1):  # сначала пара слов («вери ты»), потом одно
                if i + n > len(tokens):
                    continue
                chunk = "".join(tokens[i:i + n])
                if chunk in self.NOT_NAME or len(chunk) < 5 or len(chunk) > 8:
                    continue
                if n == 2 and (len(tokens[i]) < 3 or len(tokens[i + 1]) < 2):
                    continue
                if difflib.SequenceMatcher(None, self._norm(chunk), target).ratio() >= 0.78:
                    hit = n
                    break
            if hit:
                out.append("верити")
                i += hit
            else:
                out.append(tokens[i])
                i += 1
        return " ".join(out)

    def has_wake(self, text):
        t = text.lower()
        return bool(self.wake_re.search(t) or self.wake_re.search(self.fix_name(t)))

    def strip_wake(self, text):
        t = self.fix_name(text.lower()) if not self.wake_re.search(text.lower()) else text.lower()
        last = None
        for last in self.wake_re.finditer(t):
            pass
        if last and t[last.end():].strip(" ,.!?"):
            t = t[last.end():]  # берём то, что сказано после имени (до него могло быть эхо)
        t = self.wake_re.sub(" ", t)
        return re.sub(r"^[\s,.!?]+|[\s,.!?]+$", "", t)

    def appear(self, seconds):
        if self.glitch_wav:
            play_sound_async(self.glitch_wav)
        self.summon.emit()
        self.mode.emit("listening", float(seconds))

    # ----- перебивание -----
    def is_echo(self, text):
        """Микрофон слышит голос самой Верити: все слова фразы есть в том, что она говорит."""
        words = text.lower().replace("ё", "е").split()
        return bool(words) and all(w in self.speech_words for w in words)

    def wants_interrupt(self, text):
        t = text.lower()
        return (self.has_wake(t) or STOP_RE.search(t)) and not self.is_echo(t)

    def interrupt(self):
        if not self.speaking or self.interrupted:
            return
        self.interrupted = True
        self.speaker.stop()
        self.awake_until = time.time() + self.cfg["listen_window_seconds"]
        self.mode.emit("listening", float(self.cfg["listen_window_seconds"]))
        self.subtitle.emit(line("listen"))
        log.info("Верити перебили")

    def on_partial(self, text):
        if text and len(text) > 2:
            log.debug("Слышу (черновик): %s", text)
        now = time.time()
        if self.speaking:
            if self.cfg.get("barge_in") and self.wants_interrupt(text):
                self.interrupt()
            return
        if now - self.last_partial_wake > 3 and now > self.awake_until and self.has_wake(text):
            self.last_partial_wake = now
            self.appear(self.cfg["listen_window_seconds"])

    def on_final(self, text, audio):
        now = time.time()
        if self.actions.autoscroll_active() and STOP_RE.search(text.lower()) and not self.is_echo(text):
            # во время автопрокрутки «стоп» слышим и без имени
            self.actions.stop_background()
            self.subtitle.emit(line("scroll_stop"))
            return
        if self.speaking and not self.interrupted:
            if not (self.cfg.get("barge_in") and self.wants_interrupt(text)):
                return  # эхо собственной речи
            self.interrupt()
        if now - self.speech_end < 1.0 and self.is_echo(text):
            return
        woke = self.has_wake(text)
        log.info("Слышу: «%s» (имя %s)", text, "узнала" if woke else "не узнала")
        if not woke and now > self.awake_until:
            return
        cmd = self.strip_wake(text) if woke else text
        if STOP_RE.match(cmd.strip()) and len(cmd.split()) <= 3:
            # «Верити, стоп» — замолчать, остановить автопрокрутку и слушать дальше
            self.awake_until = now + self.cfg["listen_window_seconds"]
            if self.actions.stop_background():
                self.subtitle.emit(line("scroll_stop"))
            return
        if len(cmd) < 2:
            self.awake_until = now + self.cfg["listen_window_seconds"]
            self.subtitle.emit("")
            if now - self.last_partial_wake > 3:
                self.appear(self.cfg["listen_window_seconds"])
            return
        self.awake_until = 0
        self.jobs.put(("voice", cmd, audio, woke))

    def on_listener_error(self, err, kind="mic"):
        self.jobs.put(("say", line("model_broken" if kind == "model" else "no_mic")))

    def set_profile(self, mode, name, gender):
        """«Как ко мне обращаться…»: как называть пользователя и в каком роде. Сразу и запоминается."""
        for key, value in (("user_name_mode", mode), ("user_name", name), ("user_gender", gender)):
            self.cfg[key] = value
            save_setting(key, value)
        apply_user(self.cfg)
        log.info("Обращение: %s, %s", user_display_name(self.cfg) or "без имени", gender)

    def set_autostart(self, on):
        """Галочка в трее: запускаться вместе с Windows (ярлык в «Автозагрузке»)."""
        import autostart
        try:
            if on:
                autostart.enable()
                stray = autostart.stray_run_bat()
                if stray:  # скопированный туда run.bat не работает — убираем, чтобы не мешал
                    os.remove(stray)
            else:
                autostart.disable()
            log.info("Автозапуск: %s", "включён" if on else "выключен")
        except Exception:
            log.exception("Не удалось изменить автозапуск")

    def set_setting(self, key, value):
        """Любая настройка из окна «Настройки» — сразу в работу и в config.json."""
        self.cfg[key] = value
        save_setting(key, value)

    def set_chatter(self, on):
        """Галочка в трее: появляется ли Верити сама (поболтать, погулять по экрану). Сразу и запоминается."""
        self.cfg["chatter"] = bool(on)
        save_setting("chatter", bool(on))
        log.info("Самостоятельные появления: %s", "включены" if on else "выключены")

    def set_chat_button(self, on):
        """Тумблер в настройках: плавающая кнопка чата справа снизу. Сразу и запоминается."""
        self.cfg["chat_button"] = bool(on)
        save_setting("chat_button", bool(on))
        self.chat_button.emit(bool(on))
        log.info("Кнопка чата: %s", "включена" if on else "выключена")

    def set_search_when_unsure(self, on):
        """Галочка в трее: открывать ли Google, когда Верити не знает ответа. Сразу и запоминается."""
        self.cfg["search_when_unsure"] = bool(on)
        save_setting("search_when_unsure", bool(on))
        log.info("Поиск в браузере, если не знаю: %s", "включён" if on else "выключен")

    def _remember_model(self, name):
        """Ollama нашла другую скачанную модель — записываем её, чтобы не искать при каждом запуске."""
        self.cfg["local_model"] = name
        save_setting("local_model", name)

    # ----- config.json на лету -----
    @staticmethod
    def _config_mtime():
        try:
            return os.path.getmtime(CONFIG_PATH)
        except OSError:
            return None

    def reload_config_if_changed(self, speak=True):
        """Правки config.json (сайты, программы, названия) подхватываются без перезапуска.
        speak=False — из окна в трее: там говорить нельзя (речь блокирует окно)."""
        mtime = self._config_mtime()
        if mtime == self.config_mtime:
            return
        fresh = load_config()
        if CONFIG_ERROR:  # с ошибкой — работаем как было и говорим, где ошибка
            if speak:
                self.config_mtime = mtime
                self.say(CONFIG_ERROR)
            return
        self.config_mtime = mtime
        for key in ("sites", "apps", "app_aliases", "search_when_unsure", "browser", "chatter", "replies",
                    "user_name_mode", "user_name", "user_gender"):
            self.cfg[key] = fresh[key]
        self.actions.set_lists(self.cfg)
        apply_user(self.cfg)
        log.info("config.json перечитан")

    # ----- кнопки -----
    # ----- микрофон -----
    def current_mic(self):
        if self.listener and self.listener.active_name:
            return self.listener.active_name
        return self.cfg.get("mic_device") or default_input_name()

    def set_mic(self, name, announce=True):
        """Сменить устройство ввода на ходу и запомнить выбор (None — как в Windows по умолчанию)."""
        self.cfg["mic_device"] = name
        save_setting("mic_device", name)
        if self.listener:
            self.listener.set_device(name)
        log.info("Выбран микрофон: %s", name or "по умолчанию")
        if announce:
            self.jobs.put(("say", line("mic_set", t=name or "микрофон Windows по умолчанию")))

    def set_mic_tuning(self, gain=None, sensitivity=None):
        """Громкость ввода и чувствительность микрофона для Верити — сразу и запоминаются."""
        if gain is not None:
            self.cfg["mic_gain"] = round(float(gain), 2)
            save_setting("mic_gain", self.cfg["mic_gain"])
            if self.listener:
                self.listener.gain = self.cfg["mic_gain"]
        if sensitivity is not None:
            self.cfg["mic_sensitivity"] = int(sensitivity)
            save_setting("mic_sensitivity", self.cfg["mic_sensitivity"])
            if self.listener:
                self.listener.sensitivity = self.cfg["mic_sensitivity"]

    def set_listen_times(self, followup=None, window=None):
        """Сколько секунд слушать без имени: после ответа (0 — не слушать) и после «Верити» — сразу и запоминаются."""
        if followup is not None:
            self.cfg["followup_seconds"] = int(followup)
            save_setting("followup_seconds", int(followup))
            if followup <= 0 and not self.speaking:
                self.awake_until = 0
        if window is not None:
            self.cfg["listen_window_seconds"] = int(window)
            save_setting("listen_window_seconds", int(window))

    def set_voice_fx(self, strength):
        """Сила цифровых эффектов голоса: 0 — чистый голос, 1.8 — как задумано, больше — сильнее."""
        self.speaker.fx = round(float(strength), 2)
        self.cfg["voice_fx"] = self.speaker.fx
        save_setting("voice_fx", self.speaker.fx)

    def set_voice_volume(self, volume):
        """Громкость голоса Верити 0..100 — только её, общая громкость Windows не меняется."""
        self.speaker.volume = int(volume)
        self.cfg["voice_volume"] = int(volume)
        save_setting("voice_volume", int(volume))
        if self.glitch_wav:
            try:
                make_glitch_sound(self.glitch_wav, self.speaker.volume)
            except Exception:
                pass

    def mic_command(self, action):
        if action == "mic_info":
            return Reply(line("mic_info", t=self.current_mic()))
        devices = [n for n, _ in input_devices()]
        if len(devices) < 2:
            return Reply(line("mic_only_one", t=self.current_mic()))
        cur = self.cfg.get("mic_device") or self.current_mic()
        idx = next((i for i, n in enumerate(devices) if n == cur or cur.startswith(n) or n.startswith(cur)), -1)
        nxt = devices[(idx + 1) % len(devices)]
        self.set_mic(nxt, announce=False)
        return Reply(line("mic_set", t=nxt))

    def call_from_click(self):
        if self.speaking:
            self.interrupt()
            return
        self.awake_until = time.time() + self.cfg["listen_window_seconds"]
        self.appear(self.cfg["listen_window_seconds"])
        self.subtitle.emit(line("listen"))

    def text_command(self, text):
        self.summon.emit()
        self.jobs.put(("text", text))

    # ----- рабочий поток -----
    def _worker(self):
        try:
            import comtypes
            comtypes.CoInitialize()
        except Exception:
            pass
        while True:
            job = self.jobs.get()
            try:
                self._process(job)
            except Exception:
                log.exception("Ошибка обработки %s", job[0])
                self.say(line("crash"))

    def _process(self, job):
        kind = job[0]
        if kind == "chatter":
            text = self.chatter_line()
            if random.random() < self.cfg.get("roam_chance", 0.5):
                self.roam.emit()          # гуляет по экрану, окно с текстом сверху
                time.sleep(1.5)
            else:
                self.summon.emit()
            if not self.say(text):
                self.mode.emit("idle", 0.0)
            return
        if kind == "prank":
            import pranks
            text = pranks.run()
            if text:
                time.sleep(2.5)
                self.summon.emit()
                if not self.say(text):
                    self.mode.emit("idle", 0.0)
            return
        self.last_activity = time.time()
        if kind == "say":
            self.summon.emit()
            if len(job) > 2:
                self.anim.emit(job[2])
            if not job[1]:
                self.mode.emit("idle", 0.0)
                return
            if not self.say(job[1]):
                self.mode.emit("idle", 0.0)
            return
        if kind == "voice":
            _, cmd, audio, woke = job
            self.summon.emit()
            self.mode.emit("thinking", 0.0)
            self.subtitle.emit("> " + cmd)
            if self.cfg.get("use_google_stt"):
                better = google_stt(audio)
                if better:
                    better = merge_stt(cmd, better.lower())
                    if len(better) >= 2:
                        if better != cmd:
                            log.info("Vosk: «%s» → Google: «%s»", cmd, better)
                        cmd = better
        else:
            cmd = job[1]
        if self.has_wake(cmd):
            # имя могло остаться в тексте («верити нажми …»), если Верити уже слушала без него
            stripped = self.strip_wake(cmd)
            if len(stripped) < 2:
                # просто позвали по имени — не болтаем, а слушаем
                self.awake_until = time.time() + self.cfg["listen_window_seconds"]
                self.mode.emit("listening", float(self.cfg["listen_window_seconds"]))
                self.subtitle.emit(line("listen"))
                return
            cmd = stripped
        log.info("Команда: %s", cmd)
        if kind == "voice":  # голосовые команды тоже попадают в чат (набранные там показывает само окно)
            self.chat_message.emit("user", cmd)
        self.mode.emit("thinking", 0.0)
        self.subtitle.emit("> " + cmd)
        self.reload_config_if_changed()

        reply = self.actions.handle(cmd)
        if reply is not None and reply.action in ("mic_info", "mic_next"):
            reply = self.mic_command(reply.action)
        if reply is None:
            if self.cfg.get("enable_ai") and self.ai.available:
                reply = Reply(self.ai.ask(cmd), "answer")
            else:
                answer = self.local.ask(cmd) if self.local else None
                reply = Reply(answer, "answer") if answer else self.actions.fallback(cmd, force_search=bool(self.local))
                problem = self.local.problem if self.local and not answer else None
                if problem and problem != self.ai_problem_said:  # почему не ответила — говорим один раз
                    self.ai_problem_said = problem
                    reply = Reply(line("ai_down", why=problem) + " " + reply.text, reply.action)
        if reply.action == "answer" and self.cfg.get("teleport_on_answer"):
            self.hop.emit()  # отвечает, скача по экрану красной молнией
        if reply.action == "wave":
            self.anim.emit("wave")
        elif reply.action and reply.action.startswith("move:"):
            self.move_to.emit(reply.action[5:])
        elif reply.action and reply.action.startswith("settings:"):
            self.settings.emit(reply.action[9:])
        elif reply.action == "roam":
            self.vanish.emit()
            time.sleep(0.8)
            self.roam.emit()
            time.sleep(1.5)
        if self.say(reply.text):
            return  # перебили — уже слушаем новую команду

        if reply.action == "quit":
            self.quit_app.emit()
        elif reply.action == "hide":
            self.vanish.emit()
        else:
            follow = float(self.cfg["followup_seconds"])
            if follow <= 0:  # после ответа не слушаем: следующая команда — снова с «Верити»
                self.awake_until = 0
                self.mode.emit("idle", 0.0)
            else:
                self.awake_until = time.time() + follow
                self.mode.emit("listening", follow)

    def say(self, text):
        """Озвучивает текст. Возвращает True, если Верити перебили."""
        log.info("Верити: %s", text)
        if text and text.strip():
            self.chat_message.emit("verity", text)
        self.subtitle.emit(text)
        self.mode.emit("speaking", 0.0)
        barge_in = self.cfg.get("barge_in") and self.listener
        if self.listener and not barge_in:
            self.listener.muted.set()
        self.speech_words = set(re.findall(r"\w+", text.lower().replace("ё", "е")))
        self.interrupted = False
        self.speaking = True
        try:
            self.speaker.say(text)
        finally:
            self.speaking = False
            self.speech_end = time.time()
            if barge_in and not self.interrupted:
                self.listener.reset_req.set()  # выбрасываем эхо её голоса
            if self.listener and not barge_in:
                time.sleep(0.25)
                self.listener.muted.clear()
        self._check_voice_blocked()
        return self.interrupted

    def _check_voice_blocked(self):
        """Голос Microsoft не прошёл из-за сети, а запущен zapret — один раз за сеанс объясняем
        и предлагаем добавить сервер голоса в исключения zapret."""
        err = self.speaker.edge_error
        if self.zapret_checked or not err or not zapret.NETWORK_ERROR.search(err) or not zapret.running():
            return
        self.zapret_checked = True
        all_lists = zapret.find_all_lists()
        todo = [lst for lst in all_lists if not zapret.excluded(lst)]
        if not all_lists:
            self.jobs.put(("say", line("zapret_manual")))
        elif not todo:
            self.jobs.put(("say", line("zapret_restart")))
        else:
            def fix():
                try:
                    for lst in todo:  # во все найденные копии zapret — какая из них запущена, не видно
                        zapret.add_exclude(lst)
                        log.info("Сервер голоса добавлен в исключения zapret (имя и адреса): %s", lst)
                except OSError as e:
                    log.exception("Не удалось дописать исключение zapret")
                    return Reply(line("zapret_fail", e=e))
                return Reply(line("zapret_fixed"))
            self.actions.pending = fix
            self.awake_until = time.time() + 20  # ответ «да» без имени
            self.jobs.put(("say", line("zapret_offer")))


def tray_icon():
    pm = QPixmap(64, 64)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor(255, 205, 20))
    p.setPen(QPen(QColor(190, 120, 0), 3))
    p.drawEllipse(4, 4, 56, 56)
    p.setBrush(QColor(20, 14, 0))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawEllipse(20, 18, 7, 12)
    p.drawEllipse(37, 18, 7, 12)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(QPen(QColor(20, 14, 0), 4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    p.drawArc(16, 20, 32, 26, 200 * 16, 140 * 16)
    p.end()
    return QIcon(pm)


def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    cfg = load_config()

    try:
        skin = skins.find(os.path.join(BASE, "assets"), cfg.get("skin", ""))
        avatar = Avatar(skin)
    except Exception:  # свой скин не загрузился — стартуем со стандартной Верити, а не падаем
        log.exception("Скин %r не загрузился", cfg.get("skin"))
        avatar = Avatar(skins.builtin(os.path.join(BASE, "assets")))
    avatar.hide_after = float(cfg["hide_after_seconds"])
    apply_user(cfg)
    verity = Verity(cfg)
    verity.summon.connect(avatar.summon)
    verity.vanish.connect(avatar.vanish)
    verity.mode.connect(avatar.set_mode)
    verity.subtitle.connect(avatar.set_subtitle)
    verity.quit_app.connect(app.quit)
    verity.anim.connect(avatar.play_anim)
    verity.roam.connect(avatar.start_roam)
    verity.hop.connect(avatar.start_hopping)
    verity.move_to.connect(avatar.move_to_named)
    avatar.clicked.connect(verity.call_from_click)

    def ask_text():
        text, ok = QInputDialog.getText(None, "Верити", "Команда или вопрос:")
        if ok and text.strip():
            verity.text_command(text.strip())

    from chat_button import ChatButton
    chat_btn = ChatButton(os.path.join(BASE, "assets", "chat_button.png"))
    from chat_window import ChatWindow
    chat_win = ChatWindow(verity)
    verity.chat_message.connect(chat_win.add_message)
    verity.mode.connect(chat_win.set_mode)
    chat_btn.clicked.connect(lambda: chat_win.toggle(chat_btn))

    def chat_button_visible(on):
        chat_btn.set_visible(on)
        if not on:
            chat_win.hide()
    verity.chat_button.connect(chat_button_visible)
    if cfg.get("chat_button"):
        chat_btn.set_visible(True)

    windows = {}  # открытые окна держим, пока открыты (иначе их соберёт сборщик мусора); одно окно каждого вида

    def show_window(kind, make):
        old = windows.get(kind)
        if old is not None and old.isVisible():
            old.close()
        dlg = windows[kind] = make()
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def open_entries():
        from entries_dialog import EntriesDialog
        verity.reload_config_if_changed(speak=False)
        show_window("entries", lambda: EntriesDialog(verity.actions))

    def open_settings(tab="general"):
        from settings_window import SettingsWindow
        verity.reload_config_if_changed(speak=False)
        show_window("settings", lambda: SettingsWindow(verity, avatar, open_entries,
                                                       lambda: os.startfile(CONFIG_PATH),
                                                       lambda: verity.actions.web(VERITY_URL), tab))

    # в трее — только самое нужное, всё остальное в «Настройках»
    menu = QMenu()
    for title, fn in (("Позвать Верити", verity.call_from_click),
                      ("Замолчать", verity.interrupt),
                      ("Написать команду…", ask_text),
                      ("Настройки…", lambda: open_settings()),
                      ("Проверить ПК", lambda: verity.text_command("проверь компьютер")),
                      ("Спрятать", avatar.vanish),
                      (None, None),
                      ("Выход", app.quit)):
        if title is None:
            menu.addSeparator()
            continue
        act = QAction(title, menu)
        act.triggered.connect(fn)
        menu.addAction(act)
    verity.settings.connect(open_settings, Qt.ConnectionType.QueuedConnection)

    tray = QSystemTrayIcon(tray_icon())
    tray.setToolTip("Верити — голосовой ассистент")
    tray.setContextMenu(menu)
    tray.activated.connect(lambda r: verity.call_from_click()
                           if r == QSystemTrayIcon.ActivationReason.Trigger else None)
    tray.show()
    avatar.menu_requested.connect(lambda pos: menu.popup(pos))

    verity.start()
    sys.exit(app.exec())


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        log.exception("Верити упала при запуске")
        fatal(f"Что-то сломалось при запуске: {e}\n\nПодробности — в файле verity.log в папке Верити.")
