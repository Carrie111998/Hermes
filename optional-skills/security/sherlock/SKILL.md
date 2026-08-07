---
name: sherlock
description: Find accounts for a username across 400+ platforms.
version: 2.0.0
author: unmodeled-tyler
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [osint, security, username, social-media, reconnaissance]
    category: security
prerequisites:
  commands: [sherlock]
---

# Sherlock OSINT Username Search

Hunt down social media accounts by username across 400+ social networks using the [Sherlock Project](https://github.com/sherlock-project/sherlock).

## When to Use

- User asks to find accounts associated with a username
- User wants to check username availability across platforms
- User is conducting OSINT or reconnaissance research
- User asks "where is this username registered?" or similar

## When NOT to Use

- User wants to search by email, phone number, or real name (Sherlock only works with usernames)
- User needs deep profile data scraping (Sherlock only returns URLs)
- User wants to search private/internal platforms not in Sherlock's data source

## Requirements

- Sherlock CLI installed: `pipx install sherlock-project`
- Network access to query social platforms

## Procedure

### 1. Verify Installation

Check sherlock is available before proceeding:

```bash
sherlock --version
```

If the command fails:
- Install with `pipx install sherlock-project`
- If installation fails, inform the user and stop — do not try multiple installation methods

### 2. Extract Username

Extract the username directly from the user's message if clearly stated.

**Do NOT use clarify when:**
- "Find accounts for nasa" → username is `nasa`
- "Search for johndoe123" → username is `johndoe123`
- "Check if alice exists on social media" → username is `alice`

**Only use clarify when:**
- Multiple potential usernames mentioned ("search for alice or bob")
- Ambiguous phrasing ("search for my username" without specifying)
- No username mentioned at all ("do an OSINT search")

Preserve the exact username as stated — case, numbers, underscores, etc.

### 3. Build and Execute Command

**Default command:**
```bash
sherlock --print-found --no-color "<username>" --timeout 90
```

**Optional flags** (only add if user explicitly requests):
- `--nsfw` — Include NSFW sites
- `--tor` — Route through Tor (requires Tor daemon running)

Do NOT ask about options via clarify — run the default search. Users can request specific options if needed.

Run via the `terminal` tool with a 180-second timeout. The command typically takes 30-120 seconds.

### 4. Parse and Present Results

Sherlock outputs found accounts in this format:
```
[+] Instagram: https://instagram.com/username
[+] Twitter: https://twitter.com/username
[+] GitHub: https://github.com/username
```

Present findings as:
1. **Summary line:** "Found X accounts for username 'Y'"
2. **Categorized links:** Group by platform type if helpful (social, professional, forums, etc.)
3. **Output file location:** Sherlock saves results to `<username>.txt` by default

## Pitfalls

### No Results Found
This is often correct — the username may not be registered on checked platforms. Suggest:
- Checking spelling/variation
- Trying similar usernames with `?` wildcard: `sherlock "user?name"`
- The user may have privacy settings or deleted accounts

### Timeout Issues
Some sites are slow or block automated requests. Use `--timeout 120` to increase wait time, or `--site` to limit scope.

### Tor Configuration
`--tor` requires Tor daemon running. If user wants anonymity but Tor isn't available, suggest installing Tor service or using `--proxy` with an alternative proxy.

### False Positives
Some sites always return "found" due to their response structure. Cross-reference unexpected results with manual checks.

### Rate Limiting
Aggressive searches may trigger rate limits. For bulk username searches, add delays between calls or use `--local` with cached data.

## Installation

### pipx (recommended)
```bash
pipx install sherlock-project
```

### Alternative Methods

**pip:**
```bash
pip install sherlock-project
```

**Docker:**
```bash
docker pull sherlock/sherlock
docker run -it --rm sherlock/sherlock <username>
```

**Linux packages:** Available on Debian 13+, Ubuntu 22.10+, Homebrew, Kali, BlackArch.

## Ethical Use

This tool is for legitimate OSINT and research purposes only. Remind users:
- Only search usernames they own or have permission to investigate
- Respect platform terms of service
- Do not use for harassment, stalking, or illegal activities
- Consider privacy implications before sharing results

## Verification

After running sherlock, verify:
1. Output lists found sites with URLs
2. `<username>.txt` file created (default output) if using file output
3. If `--print-found` used, output should only contain `[+]` lines for matches
