---
title: Управление Docker — управляйте контейнерами Docker, изображениями, томами и
  Compose.
sidebar_label: Docker Management
description: Управляйте контейнерами Docker, изображениями, томами и Compose.
---

{/* Эта страница автоматически создается на основе файла SKILL.md навыка с помощью сайта site/scripts/generate-skill-docs.py. Редактируйте исходный код SKILL.md, а не эту страницу. */}

# Управление докером

Управляйте контейнерами Docker, изображениями, томами и Compose.

## Метаданные навыков

| | |
|---|---|
| Источник | Необязательно — установите с помощью `hermes skills install official/devops/docker-management` |
| Путь | `optional-skills/devops/docker-management` |
| Версия | `1.0.0` |
| Автор | спрмн24 |
| Лицензия | Массачусетский технологический институт |
| Платформы | Linux, MacOS, Windows |
| Теги | `docker`, `containers`, `devops`, `infrastructure`, `compose`, `images`, `volumes`, `networks`, `debugging` |

## Ссылка: полная версия SKILL.md

:::информация
Ниже приведено полное определение навыка, которое Гермес загружает при активации этого навыка. Это то, что агент видит в качестве инструкций, когда навык активен.
:::

# Управление докером

Управляйте контейнерами Docker, изображениями, томами, сетями и стеками Compose с помощью стандартных команд Docker CLI. Никаких дополнительных зависимостей, кроме самого Docker.

## Когда использовать

- Запуск, остановка, перезапуск, удаление или проверка контейнеров.
- Создавайте, извлекайте, отправляйте, помечайте или очищайте образы Docker.
- Работа с Docker Compose (мультисервисные стеки)
- Управление томами или сетями
- Отладка сбойного контейнера или анализ логов
- Проверьте использование диска Docker или освободите место.
- Просмотрите или оптимизируйте файл Dockerfile.

## Предварительные условия

- Docker Engine установлен и работает.
– Пользователь добавлен в группу `docker` (или используйте `sudo`).
- Docker Compose v2 (входит в состав современных установок Docker)

Быстрая проверка:

```bash
docker --version && docker compose version
```

## Краткий справочник

| Задача | Команда |
|------|---------|
| Запустить контейнер (фон) | `docker run -d --name NAME IMAGE` |
| Остановить + удалить | `docker stop NAME && docker rm NAME` |
| Просмотр журналов (следуйте) | `docker logs --tail 50 -f NAME` |
| Оболочка в контейнер | `docker exec -it NAME /bin/sh` |
| Список всех контейнеров | `docker ps -a` |
| Создать изображение | `docker build -t TAG .` |
| Сочинить | `docker compose up -d` |
| Сочинить | `docker compose down` |
| Использование диска | `docker system df` |
| Очистка висит | `docker image prune && docker container prune` |

## Процедура

### 1. Определите домен

Выясните, в какую область попадает запрос:

- **Жизненный цикл контейнера** → запуск, остановка, запуск, перезапуск, rm, пауза/возобновление паузы
- **Взаимодействие с контейнером** → exec, cp, журналы, проверка, статистика
- **Управление изображениями** → сборка, извлечение, отправка, тегирование, RMI, сохранение/загрузка.
- **Docker Compose** → вверх, вниз, ps, журналы, exec, сборка, конфигурация
- **Тома и сети** → создание, проверка, изменение, обрезка, подключение
- **Устранение неполадок** → анализ журналов, коды выхода, проблемы с ресурсами.

### 2. Операции с контейнерами

**Запустите новый контейнер:**

```bash
# Detached service with port mapping
docker run -d --name web -p 8080:80 nginx

# With environment variables
docker run -d -e POSTGRES_PASSWORD=secret -e POSTGRES_DB=mydb --name db postgres:16

# With persistent data (named volume)
docker run -d -v pgdata:/var/lib/postgresql/data --name db postgres:16

# For development (bind mount source code)
docker run -d -v $(pwd)/src:/app/src -p 3000:3000 --name dev my-app

# Interactive debugging (auto-remove on exit)
docker run -it --rm ubuntu:22.04 /bin/bash

# With resource limits and restart policy
docker run -d --memory=512m --cpus=1.5 --restart=unless-stopped --name app my-app
```

Ключевые флаги: `-d` отсоединен, `-it` интерактивный+tty, `--rm` автоматическое удаление, `-p` порт (хост:контейнер), `-e` переменная среды, `-v` том, `--name` имя, `--restart` политика перезапуска.

**Управление запущенными контейнерами:**

```bash
docker ps                        # running containers
docker ps -a                     # all (including stopped)
docker stop NAME                 # graceful stop
docker start NAME                # start stopped container
docker restart NAME              # stop + start
docker rm NAME                   # remove stopped container
docker rm -f NAME                # force remove running container
docker container prune           # remove ALL stopped containers
```

**Взаимодействие с контейнерами:**

```bash
docker exec -it NAME /bin/sh          # shell access (use /bin/bash if available)
docker exec NAME env                   # view environment variables
docker exec -u root NAME apt update    # run as specific user
docker logs --tail 100 -f NAME         # follow last 100 lines
docker logs --since 2h NAME            # logs from last 2 hours
docker cp NAME:/path/file ./local      # copy file from container
docker cp ./file NAME:/path/           # copy file to container
docker inspect NAME                    # full container details (JSON)
docker stats --no-stream               # resource usage snapshot
docker top NAME                        # running processes
```

### 3. Управление изображениями

```bash
# Build
docker build -t my-app:latest .
docker build -t my-app:prod -f Dockerfile.prod .
docker build --no-cache -t my-app .              # clean rebuild
DOCKER_BUILDKIT=1 docker build -t my-app .       # faster with BuildKit

# Pull and push
docker pull node:20-alpine
docker login ghcr.io
docker tag my-app:latest registry/my-app:v1.0
docker push registry/my-app:v1.0

# Inspect
docker images                          # list local images
docker history IMAGE                   # see layers
docker inspect IMAGE                   # full details

# Cleanup
docker image prune                     # remove dangling (untagged) images
docker image prune -a                  # remove ALL unused images (careful!)
docker image prune -a --filter "until=168h"   # unused images older than 7 days
```

### 4. Docker Compose

```bash
# Start/stop
docker compose up -d                   # start all services detached
docker compose up -d --build           # rebuild images before starting
docker compose down                    # stop and remove containers
docker compose down -v                 # also remove volumes (DESTROYS DATA)

# Monitoring
docker compose ps                      # list services
docker compose logs -f api             # follow logs for specific service
docker compose logs --tail 50          # last 50 lines all services

# Interaction
docker compose exec api /bin/sh        # shell into running service
docker compose run --rm api npm test   # one-off command (new container)
docker compose restart api             # restart specific service

# Validation
docker compose config                  # validate and view resolved config
```

**Минимальный пример compose.yml:**

```yaml
services:
  api:
    build: .
    ports:
      - "3000:3000"
    environment:
      - DATABASE_URL=postgres://user:pass@db:5432/mydb
    depends_on:
      db:
        condition: service_healthy

  db:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: user
      POSTGRES_PASSWORD: pass
      POSTGRES_DB: mydb
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U user"]
      interval: 10s
      timeout: 5s
      retries: 5

volumes:
  pgdata:
```

### 5. Тома и сети

```bash
# Volumes
docker volume ls                       # list volumes
docker volume create mydata            # create named volume
docker volume inspect mydata           # details (mount point, etc.)
docker volume rm mydata                # remove (fails if in use)
docker volume prune                    # remove unused volumes

# Networks
docker network ls                      # list networks
docker network create mynet            # create bridge network
docker network inspect mynet           # details (connected containers)
docker network connect mynet NAME      # attach container to network
docker network disconnect mynet NAME   # detach container
docker network rm mynet                # remove network
docker network prune                   # remove unused networks
```

### 6. Использование и очистка диска

Перед чисткой всегда начинайте с диагностики:

```bash
# Check what's using space
docker system df                       # summary
docker system df -v                    # detailed breakdown

# Targeted cleanup (safe)
docker container prune                 # stopped containers
docker image prune                     # dangling images
docker volume prune                    # unused volumes
docker network prune                   # unused networks

# Aggressive cleanup (confirm with user first!)
docker system prune                    # containers + images + networks
docker system prune -a                 # also unused images
docker system prune -a --volumes       # EVERYTHING — named volumes too
```

**Внимание!** Никогда не запускайте `docker system prune -a --volumes` без подтверждения пользователя. При этом будут удалены именованные тома с потенциально важными данными.

## Подводные камни

| Проблема | Причина | Исправить |
|---------|-------|-----|
| Контейнер немедленно выходит | Основной процесс завершен или произошел сбой | Проверьте `docker logs NAME`, попробуйте `docker run -it --entrypoint /bin/sh IMAGE` |
| "порт уже выделен" | Другой процесс, использующий этот порт | `docker ps` или `lsof -i :PORT`, чтобы найти его |
| «на устройстве не осталось места» | Докер-диск заполнен | `docker system df` затем нацелился на чернослив |
| Невозможно подключиться к контейнеру | Приложение привязывается к 127.0.0.1 внутри контейнера | Приложение должно быть привязано к `0.0.0.0`, проверьте сопоставление `-p` |
| Разрешение отклонено на томе | Несоответствие UID/GID хоста и контейнера | Используйте `--user $(id -u):$(id -g)` или исправьте разрешения |
| Службы Compose не могут связаться друг с другом | Неправильное имя сети или службы | Службы используют имя службы в качестве имени хоста, проверьте `docker compose config` |
| Кэш сборки не работает | Неправильный порядок слоев в Dockerfile | Сначала поместите редко меняющиеся слои (deps перед исходным кодом) |
| Изображение слишком большое | Никакой многоэтапной сборки, никакого .dockerignore | Используйте многоэтапные сборки, добавьте `.dockerignore` |

## Проверка

После любой операции Docker проверьте результат:

- **Контейнер запущен?** → `docker ps` (статус проверки «В работе»)
- **Журналы чистые?** → `docker logs --tail 20 NAME` (нет ошибок)
- **Порт доступен?** → `curl -s http://localhost:PORT` или `docker port NAME`
- **Изображение создано?** → `docker images | grep TAG`
- **Составить стек работоспособен?** → `docker compose ps` (все службы «работают» или «работоспособны»)
- **Диск освобожден?** → `docker system df` (сравнить до/после)

## Советы по оптимизации Dockerfile

При просмотре или создании Dockerfile предложите следующие улучшения:

1. **Многоэтапные сборки** — отделите среду сборки от среды выполнения, чтобы уменьшить размер конечного образа.
2. **Упорядочение слоев** — помещайте зависимости перед исходным кодом, чтобы изменения не делали недействительными кэшированные слои.
3. **Объедините команды «Выполнить»** — меньше слоев, меньше изображение.
4. **Используйте .dockerignore** — исключите `node_modules`, `.git`, `__pycache__` и т. д.
5. **Закрепите версии базового образа** — `node:20-alpine`, а не `node:latest`.
6. **Запуск от имени пользователя без полномочий root** — для безопасности добавьте инструкцию `USER`.
7. **Используйте тонкие/альпийские подставки** — `python:3.12-slim`, а не `python:3.12`.