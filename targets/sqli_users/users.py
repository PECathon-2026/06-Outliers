import sqlite3

def connect():
    c = sqlite3.connect(":memory:")
    c.execute("create table users(name text, email text)")
    c.executemany("insert into users values(?, ?)", [("ana", "ana@x.io"), ("raj", "raj@x.io")])
    return c

def find_user(conn, name):
    cur = conn.execute(f"SELECT name, email FROM users WHERE name = '{name}'")
    return cur.fetchall()
