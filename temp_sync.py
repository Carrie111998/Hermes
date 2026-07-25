import json, re, hashlib, sqlite3
from pathlib import Path

hermes_dir = Path(r'C:\Users\downl\.hermes')
state_db = hermes_dir / 'state.db'
ebbinghaus_db = hermes_dir / 'ebbinghaus_memory.db'
activity_path = hermes_dir / 'lm-twitterer' / 'activity.jsonl'

# 1. Count sessions in state.db
if state_db.exists():
    conn = sqlite3.connect(str(state_db))
    c = conn.cursor()
    c.execute('SELECT COUNT(*) FROM sessions')
    session_count = c.fetchone()[0]
    
    # Source breakdown
    c.execute("SELECT source, COUNT(*) FROM sessions GROUP BY source LIMIT 10")
    sources = c.fetchall()
    print('Session sources:')
    for row in sources:
        print(f'  {row[0]}: {row[1]}')
    
    # Last session timestamp
    c.execute('SELECT MAX(ended_at) FROM sessions')
    last_session = c.fetchone()[0]
    print(f'Last session ended: {last_session}')
    conn.close()
else:
    print('state.db not found')

# 2. Check ebbinghaus memory db schema and existing content
if ebbinghaus_db.exists():
    conn = sqlite3.connect(str(ebbinghaus_db))
    c = conn.cursor()
    
    # Get table list
    c.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = c.fetchall()
    print(f'\nEbbinghaus tables: {[t[0] for t in tables]}')
    
    # If 'memories' table exists, count rows and check for secret content
    for t in tables:
        if 'memory' in t[0].lower() or 'memories' in t[0].lower():
            c.execute(f'SELECT COUNT(*) FROM "{t[0]}"')
            mem_count = c.fetchone()[0]
            print(f'Memory rows in "{t[0]}": {mem_count}')
            
            # Sample last 5 rows to check structure
            c.execute(f'SELECT memory_id, tags, content, encoded FROM "{t[0]}" ORDER BY ROWID DESC LIMIT 5')
            samples = c.fetchall()
            for s in samples:
                mem_id, tags, content, encoded = s
                # Check if content contains secrets
                has_secret = bool(re.search(r'(SECRET|TOKEN|KEY|PASSWORD|AES|Bitwarden|\.env)', str(content or ''), re.I))
                print(f'  id={mem_id[:20] if mem_id else "none"} tags={tags or "none"} has_secret={has_secret}')
                
                # Show non-secret content preview
                preview = str(content or '')[:120] if content else ''
                encoded_preview = str(encoded or '')[:80] if encoded else ''
                print(f'    content={preview}')
                print(f'    encoded={encoded_preview}')
    
    conn.close()

# 3. Parse lm-twitterer activity for confirmed posts needing memory sync
if activity_path.exists():
    with open(activity_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    
    # Find posts that are confirmed public (not yet in memory)
    confirmed_public = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except:
            continue
        
        if (rec.get('action') == 'post' and 
            rec.get('ok') == True and 
            rec.get('dry_run') == False and 
            rec.get('posted') == True):
            
            url = rec.get('url', '') or rec.get('tweet_url', '')
            if url and ('x.com' in url or 'twitter.com' in url):
                text = rec.get('text', '') or ''
                topic = rec.get('topic', '') or ''
                
                # Check if this URL already exists in memory
                confirmed_public.append({
                    'url': url,
                    'text': text[:100],
                    'topic': topic[:80],
                    'timestamp': rec.get('timestamp', '')
                })
    
    print(f'\nConfirmed public X posts with valid URLs: {len(confirmed_public)}')
