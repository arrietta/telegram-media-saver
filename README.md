# Telegram Media Saver

Бот для Telegram: присылаешь ссылку на **Instagram**, **YouTube** или **TikTok** —
он отвечает в чат самим видео, фото или альбомом. Работает в личке и в группах.

- видео — через [yt-dlp](https://github.com/yt-dlp/yt-dlp), фото и карусели — через [gallery-dl](https://github.com/mikf/gallery-dl);
- карусели уходят одним альбомом (до 10 файлов);
- YouTube: бот сразу присылает видео в лучшем качестве до 720p, которое влезает в лимит Telegram,
  с названием ролика в подписи. Если ролик слишком длинный для 50 МБ или что-то пошло не так —
  объясняет причину и даёт кнопку на ssyoutube.com;
- TikTok: видео, а также фото-посты (слайд-шоу) — все фото альбомом и музыка отдельным файлом,
  в том числе по коротким ссылкам `vm.tiktok.com`. Если TikTok блокирует IP сервера, бот берёт
  медиа через сторонний сервис [tikwm.com](https://www.tikwm.com) (ключ не нужен).

## 1. Создать бота

1. Напиши [@BotFather](https://t.me/BotFather) → `/newbot` → получи токен.
2. Чтобы бот видел ссылки в группах: `/setprivacy` → выбери бота → **Disable**.

## 2. Запуск локально

Нужны Python 3.11+ и ffmpeg (`brew install ffmpeg`; для YouTube ещё `brew install deno`).

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # впиши BOT_TOKEN
.venv/bin/python bot.py
```

## 3. Бесплатный хостинг: GitHub + Render

1. Залей эту папку в репозиторий на GitHub.
2. На [render.com](https://render.com) войди через GitHub → **New → Blueprint** → выбери репозиторий
   (настройки возьмутся из `render.yaml`).
3. Впиши `BOT_TOKEN`, когда Render спросит, и нажми **Apply**.

Дальше каждый `git push` сам выкатывает новую версию. Webhook бот ставит сам.

## Ограничения, о которых стоит знать

- **50 МБ на файл** — лимит Telegram для ботов. Длинные ролики с YouTube не пройдут.
- **Бесплатный Render засыпает** через 15 минут без запросов. Первая ссылка после паузы
  обрабатывается с задержкой около минуты, пока сервис просыпается.
- **YouTube и Instagram часто блокируют серверные IP** («Sign in to confirm you're not a bot»,
  «login required»). Лечится файлом cookies: экспортируй `cookies.txt` (формат Netscape)
  из браузера, где ты залогинен, и добавь его в Render как **Secret File** с именем `cookies.txt`
  (Environment → Secret Files) — бот найдёт его сам. Лучше использовать запасной аккаунт.
  TikTok обычно работает без этого.
- **YouTube через Cloudflare WARP (эксперимент).** При запуске бот пробует поднять бесплатный
  туннель WARP (`warp.py`) и ходит на YouTube через него — с адреса Cloudflare, а не хостинга.
  В логах при старте будет строка `WARP is ready` или `WARP is off: причина`. Если туннель
  не поднялся или YouTube отказал и ему, бот пробует напрямую с `cookies.txt`.
- yt-dlp нужно периодически обновлять — сайты меняются. На Render для этого достаточно
  **Manual Deploy → Clear build cache & deploy**.
