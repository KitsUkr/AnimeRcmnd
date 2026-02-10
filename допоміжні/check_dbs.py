import sqlite3

print("=== POSTERS.DB ===")
conn1 = sqlite3.connect('posters.db')
cursor1 = conn1.cursor()
cursor1.execute("SELECT COUNT(*) FROM posters WHERE telegram_ok = 1")
print("Count with telegram_ok=1:", cursor1.fetchone()[0])
cursor1.execute("SELECT hikka_url, full_url FROM posters WHERE telegram_ok = 1 LIMIT 5")
print("Sample valid posters:")
for row in cursor1.fetchall():
    print(f"  - hikka_url: {row[0]}")
    print(f"    full_url: {row[1][:80]}...")
conn1.close()

print("\n=== BOT.SQLITE3 ===")
conn2 = sqlite3.connect('bot.sqlite3')
cursor2 = conn2.cursor()
cursor2.execute("PRAGMA table_info(anime_library)")
cols = cursor2.fetchall()
print("anime_library columns:")
for col in cols:
    print(f"  - {col[1]} ({col[2]})")
cursor2.execute("SELECT slug, poster FROM anime_library LIMIT 3")
print("\nSample data (slug, poster):")
for row in cursor2.fetchall():
    print(f"  - slug: {row[0]}, poster: {row[1][:60] if row[1] else 'NULL'}...")
conn2.close()
