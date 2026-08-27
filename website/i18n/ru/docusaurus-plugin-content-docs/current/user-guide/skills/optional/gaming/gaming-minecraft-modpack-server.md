---
title: Сервер Minecraft Modpack — хост-серверы Minecraft с модификациями (CurseForge,
  Modrinth)
sidebar_label: Minecraft Modpack Server
description: Хостинг модифицированных серверов Minecraft (CurseForge, Modrinth)
---

{/* Эта страница автоматически создается на основе файла SKILL.md навыка с помощью сайта site/scripts/generate-skill-docs.py. Редактируйте исходный код SKILL.md, а не эту страницу. */}

# Сервер модпаков Minecraft

Хостинг модифицированных серверов Minecraft (CurseForge, Modrinth).

## Метаданные навыков

| | |
|---|---|
| Источник | Необязательно — установите с помощью `hermes skills install official/gaming/minecraft-modpack-server` |
| Путь | `optional-skills/gaming/minecraft-modpack-server` |
| Версия | `1.0.0` |
| Автор | Текниум (текниум1), Агент Гермеса |
| Лицензия | Массачусетский технологический институт |
| Платформы | Linux, MacOS |

## Ссылка: полная версия SKILL.md

:::информация
Ниже приведено полное определение навыка, которое Гермес загружает при активации этого навыка. Это то, что агент видит в качестве инструкций, когда навык активен.
:::

# Настройка сервера модпаков Minecraft

## Когда использовать
- Пользователь хочет настроить модифицированный сервер Minecraft из zip-архива серверного пакета.
- Пользователю нужна помощь с настройкой сервера NeoForge/Forge.
- Пользователь спрашивает о настройке производительности сервера Minecraft или резервном копировании.

## Сначала соберите пользовательские настройки
Перед началом настройки запросите у пользователя:
- **Имя сервера/MOTD** — что должно быть указано в списке серверов?
- **Seed** — определенное семя или случайное?
- **Сложность** — мирная/легкая/нормальная/сложная?
- **Режим игры** — выживание/творчество/приключение?
- **Онлайн-режим** — true (аутентификация Mojang, легальные учетные записи) или false (локальная сеть/взломанная версия)?
- **Количество игроков** — сколько игроков ожидается? (влияет на ОЗУ и настройку расстояния просмотра)
- **Распределение ОЗУ** — или позволить агенту решать на основе количества модов и доступной ОЗУ?
- **Расстояние просмотра/дистанция симуляции** — или позволить агенту выбирать на основе количества игроков и оборудования?
- **PvP** — включено или выключено?
- **Белый список** — открытый сервер или только белый список?
- **Резервные копии** — хотите автоматически создавать резервные копии? Как часто?

Используйте разумные значения по умолчанию, если пользователю все равно, но всегда спрашивайте, прежде чем создавать конфигурацию.

## Шаги

### 1. Загрузите и проверьте пакет
```bash
mkdir -p ~/minecraft-server
cd ~/minecraft-server
wget -O serverpack.zip "<URL>"
unzip -o serverpack.zip -d server
ls server/
```
Найдите: `startserver.sh`, jar-файл установщика (neoforge/forge), `user_jvm_args.txt`, папку `mods/`.
Проверьте скрипт, чтобы определить: тип загрузчика мода, версию и требуемую версию Java.

### 2. Установите Java
- Майнкрафт 1.21+ → Java 21: `sudo apt install openjdk-21-jre-headless`
- Майнкрафт 1.18-1.20 → Java 17: `sudo apt install openjdk-17-jre-headless`
- Minecraft 1.16 и более ранние версии → Java 8: `sudo apt install openjdk-8-jre-headless`
– Проверьте: `java -version`

### 3. Установите загрузчик модов
Большинство серверных пакетов включают сценарий установки. Используйте переменную окружения INSTALL_ONLY для установки без запуска:
```bash
cd ~/minecraft-server/server
ATM10_INSTALL_ONLY=true bash startserver.sh
# Or for generic Forge packs:
# java -jar forge-*-installer.jar --installServer
```
Это загружает библиотеки, исправляет jar сервера и т. д.

### 4. Примите лицензионное соглашение
```bash
echo "eula=true" > ~/minecraft-server/server/eula.txt
```

### 5. Настройте server.properties
Ключевые настройки для мода/LAN:
```properties
motd=\u00a7b\u00a7lServer Name \u00a7r\u00a78| \u00a7aModpack Name
server-port=25565
online-mode=true          # false for LAN without Mojang auth
enforce-secure-profile=true  # match online-mode
difficulty=hard            # most modpacks balance around hard
allow-flight=true          # REQUIRED for modded (flying mounts/items)
spawn-protection=0         # let everyone build at spawn
max-tick-time=180000       # modded needs longer tick timeout
enable-command-block=true
```

Настройки производительности (масштабирование до аппаратного обеспечения):
```properties
# 2 players, beefy machine:
view-distance=16
simulation-distance=10

# 4-6 players, moderate machine:
view-distance=10
simulation-distance=6

# 8+ players or weaker hardware:
view-distance=8
simulation-distance=4
```

### 6. Настройте аргументы JVM (user_jvm_args.txt)
Масштабируйте ОЗУ в зависимости от количества игроков и модов. Эмпирическое правило для модов:
- 100-200 модов: 6-12Гб
- 200-350+ модов: 12-24ГБ
- Оставьте минимум 8 ГБ свободного места для ОС/других задач.

```
-Xms12G
-Xmx24G
-XX:+UseG1GC
-XX:+ParallelRefProcEnabled
-XX:MaxGCPauseMillis=200
-XX:+UnlockExperimentalVMOptions
-XX:+DisableExplicitGC
-XX:+AlwaysPreTouch
-XX:G1NewSizePercent=30
-XX:G1MaxNewSizePercent=40
-XX:G1HeapRegionSize=8M
-XX:G1ReservePercent=20
-XX:G1HeapWastePercent=5
-XX:G1MixedGCCountTarget=4
-XX:InitiatingHeapOccupancyPercent=15
-XX:G1MixedGCLiveThresholdPercent=90
-XX:G1RSetUpdatingPauseTimePercent=5
-XX:SurvivorRatio=32
-XX:+PerfDisableSharedMem
-XX:MaxTenuringThreshold=1
```

### 7. Откройте брандмауэр
```bash
sudo ufw allow 25565/tcp comment "Minecraft Server"
```
Свяжитесь с: `sudo ufw status | grep 25565`

### 8. Создайте сценарий запуска
```bash
cat > ~/start-minecraft.sh << 'EOF'
#!/bin/bash
cd ~/minecraft-server/server
java @user_jvm_args.txt @libraries/net/neoforged/neoforge/<VERSION>/unix_args.txt nogui
EOF
chmod +x ~/start-minecraft.sh
```
Примечание. Для Forge (не NeoForge) путь к файлу args отличается. Проверьте `startserver.sh` на предмет точного пути.

### 9. Настройте автоматическое резервное копирование
Создайте скрипт резервного копирования:
```bash
cat > ~/minecraft-server/backup.sh << 'SCRIPT'
#!/bin/bash
SERVER_DIR="$HOME/minecraft-server/server"
BACKUP_DIR="$HOME/minecraft-server/backups"
WORLD_DIR="$SERVER_DIR/world"
MAX_BACKUPS=24
mkdir -p "$BACKUP_DIR"
[ ! -d "$WORLD_DIR" ] && echo "[BACKUP] No world folder" && exit 0
TIMESTAMP=$(date +%Y-%m-%d_%H-%M-%S)
BACKUP_FILE="$BACKUP_DIR/world_${TIMESTAMP}.tar.gz"
echo "[BACKUP] Starting at $(date)"
tar -czf "$BACKUP_FILE" -C "$SERVER_DIR" world
SIZE=$(du -h "$BACKUP_FILE" | cut -f1)
echo "[BACKUP] Saved: $BACKUP_FILE ($SIZE)"
BACKUP_COUNT=$(ls -1t "$BACKUP_DIR"/world_*.tar.gz 2>/dev/null | wc -l)
if [ "$BACKUP_COUNT" -gt "$MAX_BACKUPS" ]; then
    REMOVE=$((BACKUP_COUNT - MAX_BACKUPS))
    ls -1t "$BACKUP_DIR"/world_*.tar.gz | tail -n "$REMOVE" | xargs rm -f
    echo "[BACKUP] Pruned $REMOVE old backup(s)"
fi
echo "[BACKUP] Done at $(date)"
SCRIPT
chmod +x ~/minecraft-server/backup.sh
```

Добавьте почасовой cron:
```bash
(crontab -l 2>/dev/null | grep -v "minecraft/backup.sh"; echo "0 * * * * $HOME/minecraft-server/backup.sh >> $HOME/minecraft-server/backups/backup.log 2>&1") | crontab -
```

## Подводные камни
- ВСЕГДА устанавливайте `allow-flight=true` для модов — в противном случае моды с реактивными ранцами/полетами будут кикнуть игроков.
- `max-tick-time=180000` или выше — модифицированные серверы часто имеют длинные тики во время генерации мира.
- Первый запуск МЕДЛЕННЫЙ (несколько минут для больших пакетов) — не паникуйте.
- «Не успеваю!» предупреждения при первом запуске являются нормальными, исчезают после первоначальной генерации фрагмента
- Если online-mode=false, также установите Enforce-secure-profile=false, иначе клиенты будут отклонены.
- В файле startserver.sh пакета часто есть цикл автоматического перезапуска — создайте чистый сценарий запуска без него.
- Удалите мир/папку для регенерации с новым семенем.
- Некоторые пакеты имеют переменные окружения для управления поведением (например, ATM10 использует ATM10_JAVA, ATM10_RESTART, ATM10_INSTALL_ONLY)

## Проверка
- `pgrep -fa neoforge` или `pgrep -fa minecraft`, чтобы проверить, работает ли
– Проверьте журналы: `tail -f ~/minecraft-server/server/logs/latest.log`.
- Найдите «Готово (Xs)!» в логе = сервер готов
- Тестовое соединение: игрок добавляет IP-адрес сервера в сетевой игре.