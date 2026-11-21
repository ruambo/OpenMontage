#!/usr/bin/env python3
# [CEIL5] bridge /jobs 透传 lane 字段（"fast"→PG500 快道；缺省=主道，行为不变）
import pathlib
p = pathlib.Path("/home/ruanlinbo/harness/h3/h3_bridge.py")
s = p.read_text()
if '"lane"' in s:
    print("already patched")
    raise SystemExit(0)
o = '                "filename_prefix": job_id,'
n = ('                "filename_prefix": job_id,\n'
     '                "lane": str(body.get("lane", "")),  # [CEIL5] "fast"=PG500 快道(强制 te=small)，缺省主道')
assert s.count(o) == 1, s.count(o)
p.write_text(s.replace(o, n))
import py_compile
py_compile.compile(str(p), doraise=True)
print("bridge lane passthrough OK")
