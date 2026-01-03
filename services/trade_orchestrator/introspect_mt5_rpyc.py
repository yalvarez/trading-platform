import rpyc

conn = rpyc.connect("atp-mt5-acct1", 8001, config={"sync_request_timeout": 30})
root = conn.root
print("Root type:", type(root))
attrs = dir(root)
print("dir(root):", attrs)
for attr in attrs:
    if not attr.startswith("_"):
        try:
            val = getattr(root, attr)
            print(f"{attr}: {type(val)}")
        except Exception as e:
            print(f"{attr}: ERROR - {e}")
conn.close()
