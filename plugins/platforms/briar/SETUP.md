# Briar Setup Guide

## What is briar-headless?

`briar-headless` is a headless Briar peer. It runs the Briar mesh network
without a phone GUI and exposes a local REST/WebSocket API, so tools like
Hermes can send and receive Briar messages programmatically.

Default API endpoint: `http://127.0.0.1:7000`

## Prerequisites

- Java Runtime Environment (JRE) 8 or later
- Internet access for Briar onion-routed peer discovery

## Install briar-headless

`briar-headless` ships as a platform-specific JAR built from the official
Briar repository.

### Linux (x86_64)

```bash
sudo apt install default-jre
git clone git@code.briarproject.org:briar/briar.git
cd briar
./gradlew --configure-on-demand briar-headless:x86LinuxJar
java -jar briar-headless/build/libs/briar-headless-linux-x86_64.jar
```

### Linux (aarch64 / arm64)

```bash
./gradlew --configure-on-demand briar-headless:aarch64LinuxJar
java -jar briar-headless/build/libs/briar-headless-linux-aarch64.jar
```

### macOS (Intel x86_64)

```bash
brew install openjdk
./gradlew --configure-on-demand briar-headless:x86MacOsJar
java -jar briar-headless/build/libs/briar-headless-macos-x86_64.jar
```

On macOS you must also sign the bundled native Tor binaries before running:
extract the `aarch64` or `x86_64` native libs from the JAR, `codesign` them,
and replace the originals inside the JAR.

### macOS (Apple Silicon aarch64)

```bash
./gradlew --configure-on-demand briar-headless:aarch64MacOsJar
java -jar briar-headless/build/libs/briar-headless-macos-aarch64.jar
```

### Windows

```powershell
./gradlew --configure-on-demand briar-headless:windowsJar
java -jar briar-headless\build\libs\briar-headless-windows.jar
```

Build requires Git Bash or WSL with Gradle.

Official source: https://code.briarproject.org/briar/briar  
GitHub mirror: https://github.com/briar/briar

## First run

On first start `briar-headless` asks for a nickname and password:

```text
No account found. Let's create one!

Nickname: testuser
Password:
```

After that it starts silently. Use `-v` for verbose logging.

Data lives in `~/.briar/` by default.

## Where to find the values Hermes needs

### API URL

Usually `http://127.0.0.1:7000` if `briar-headless` runs on the same machine.

### Bearer token

```bash
cat ~/.briar/auth_token
```

The token is auto-generated on first run.

### Contact ID

Run:

```bash
curl -s -H "Authorization: Bearer $(cat ~/.briar/auth_token)" \
  http://127.0.0.1:7000/v1/contacts
```

Use the numeric `contactId` from the JSON response of the contact you want
Hermes to use as the default conversation partner.

## Hermes setup

```bash
hermes setup gateway
# choose Briar, then enter:
#   API URL: http://127.0.0.1:7000
#   Default Briar contact ID: <numeric contactId>
#   briar-headless bearer token: <contents of ~/.briar/auth_token>
```

If `briar-headless` is already running, Hermes will try to auto-detect it
and pre-fill the token and contact list.

## Troubleshooting

- `401 Unauthorized` — wrong or missing bearer token. Check `~/.briar/auth_token`.
- `connection refused` — `briar-headless` is not running. Start it in another
  terminal or install it first.
- No contacts shown — add contacts through another Briar client first, or use
  the Briar desktop/phone app to exchange contact links.
