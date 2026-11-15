# Права человека — мероприятия в Варшаве

Приложение, которое само собирает мероприятия НКО и фондов в Варшаве. Каждый день робот GitHub Actions
заходит на сайты организаций, обновляет `events.json` и сайт показывает свежую подборку.

## Что в репозитории

| Файл | Зачем |
|---|---|
| `index.html` | само приложение |
| `admin.html` | админка: добавлять, править и удалять события с телефона |
| `events.json` | готовая подборка, её обновляет робот |
| `manual.json` | события, добавленные вручную (робот их не трогает) |
| `sources.json` | с каких сайтов собирать |
| `collect.py`, `requirements.txt` | робот-сборщик |
| `.github/workflows/collect.yml` | расписание: каждый день в 5:17 |
| `manifest.webmanifest`, `service-worker.js`, `*.png` | установка на телефон и работа без интернета |

## Как выложить (с телефона, через github.com в Safari)

1. Войдите в нужный аккаунт → «+» → **New repository** → имя, например `meropriyatiya` → **Public** → **Create repository**.
2. **uploading an existing file** → выберите все файлы из распакованного архива, кроме папки `.github` → **Commit changes**.
3. Файл расписания: **Add file → Create new file**. В поле имени напишите ровно
   `.github/workflows/collect.yml` (слеши сами создадут папки), вставьте содержимое файла `collect.yml` → **Commit changes**.
4. **Settings → Actions → General → Workflow permissions → Read and write permissions → Save.**
5. **Settings → Pages → Deploy from a branch → main, / (root) → Save.** Через 1–2 минуты сайт откроется по адресу
   `https://ЛОГИН.github.io/meropriyatiya/`.
6. Первый сбор: вкладка **Actions** → «Собрать мероприятия» → **Run workflow**. Через минуту-две в `events.json`
   появится свежая подборка, а в поле `status` видно, какой сайт ответил (`ok: 5`) и какой нет.

Установка на телефон: откройте адрес в Safari → «Поделиться» → «На экран "Домой"».

## Откуда робот собирает сейчас

- Polskie Forum Migracyjne — календарь событий Centrum Wielokulturowego
- Centrum Praw Kobiet — календарь Akademii Mocy Kobiet
- RPO — события под патронатом и последние новости
- Fundacja Batorego — страница «Wydarzenia»

Остальные организации (Amnesty, Humanity in Action, SCWO, Niebieska Linia и др.) не публикуют календарь
в удобном для робота виде — их события добавляются в `manual.json`.

## Как добавить событие вручную

**Проще всего — через админку** `https://ЛОГИН.github.io/meropriyatiya/admin.html`:
1. Один раз создайте ключ: github.com → фото профиля → Settings → Developer settings → Personal access tokens →
   **Fine-grained tokens** → Generate new token → Repository access: **Only select repositories** → этот репозиторий →
   Permissions → Repository → **Contents: Read and write** → Generate. Скопируйте ключ.
2. В админке откройте «Подключение к GitHub», впишите аккаунт, репозиторий и ключ → «Подключить».
   Ключ хранится только на этом телефоне.
3. Заполните форму → «Сохранить событие». Робот запустится сам, и через 1–2 минуты событие появится в приложении.

Ключ никому не пересылайте и не вставляйте в файлы репозитория. Если телефон потеряется — удалите ключ на GitHub
(там же, где создавали).

Вручную, без админки: откройте `manual.json` → значок карандаша → скопируйте любое событие и поменяйте поля:
`id` (любой уникальный), `org` (id вкладки: rpo, amnesty, hia, sip, hfpc, pfm, ocalenie, cpk, kph, bialdom,
batory, panoptykon, pah, fdds, scwo, niebieska, city, other), `title`, `start` (ГГГГ-ММ-ДД), `end`, `time`,
`place`, `host`, `tags`, `url`, `note`. Сохраните — на следующем запуске событие появится в приложении
(или запустите робота вручную, шаг 6).

## Как добавить новый сайт

Добавьте запись в `sources.json`. Поддерживаются:
- `tribe` — сайты на WordPress с плагином The Events Calendar (нужен только адрес сайта в `base`);
- `ics` — любая ссылка на календарь `.ics`;
- `jsonld_list` — страница-список событий; `link_re` — шаблон ссылок на события.

Если сайт сменит вёрстку и робот перестанет его понимать, события этого источника останутся из прошлого
сбора, а в `status` появится «ошибка».

## Важно

GitHub отключает расписание, если в репозитории 60 дней не было изменений. Робот сам коммитит
`events.json`, когда находит новое, но если он долго ничего не находит — зайдите в Actions и нажмите
**Enable workflow**.
