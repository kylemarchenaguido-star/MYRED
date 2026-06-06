CASES = r'''
$ ./client get nokey
(nil)
$ ./client set mykey hello
(nil)
$ ./client get mykey
(str) hello
$ ./client set mykey world
(nil)
$ ./client get mykey
(str) world
$ ./client del mykey
(int) 1
$ ./client del mykey
(int) 0
$ ./client get mykey
(nil)
$ ./client keys
(arr) len=0
(arr) end
$ ./client set k1 v1
(nil)
$ ./client set k2 v2
(nil)
$ ./client keys
(arr) len=2
(str) k2
(str) k1
(arr) end
$ ./client del k1
(int) 1
$ ./client del k2
(int) 1
$ ./client pexpire nokey 5000
(int) 0
$ ./client set ttlkey hello
(nil)
$ ./client pttl ttlkey
(int) -1
$ ./client pexpire ttlkey 10000
(int) 1
$ ./client del ttlkey
(int) 1
$ ./client zscore asdf n1
(nil)
$ ./client zquery xxx 1 asdf 0 10
(arr) len=0
(arr) end
$ ./client zadd zset 1 n1
(int) 1
$ ./client zadd zset 2 n2
(int) 1
$ ./client zadd zset 3 n3
(int) 1
$ ./client zadd zset 1.1 n1
(int) 0
$ ./client zscore zset n1
(dbl) 1.1
$ ./client zscore zset n2
(dbl) 2
$ ./client zscore zset noexist
(nil)
$ ./client zquery zset 1 "" 0 10
(arr) len=6
(str) n1
(dbl) 1.1
(str) n2
(dbl) 2
(str) n3
(dbl) 3
(arr) end
$ ./client zquery zset 1.1 "" 1 10
(arr) len=4
(str) n2
(dbl) 2
(str) n3
(dbl) 3
(arr) end
$ ./client zquery zset 1.1 "" 3 10
(arr) len=0
(arr) end
$ ./client zrevquery zset 9999 "" 0 10
(arr) len=6
(str) n3
(dbl) 3
(str) n2
(dbl) 2
(str) n1
(dbl) 1.1
(arr) end
$ ./client zrank zset n1
(int) 0
$ ./client zrank zset n2
(int) 1
$ ./client zrank zset n3
(int) 2
$ ./client zrank zset noexist
(nil)
$ ./client zrem zset noexist
(int) 0
$ ./client zrem zset n1
(int) 1
$ ./client zscore zset n1
(nil)
$ ./client zquery zset 1 "" 0 10
(arr) len=4
(str) n2
(dbl) 2
(str) n3
(dbl) 3
(arr) end
$ ./client zrem zset n2
(int) 1
$ ./client zrem zset n3
(int) 1
'''


import shlex
import subprocess

cmds = []
outputs = []
lines = CASES.splitlines()
for x in lines:
    x = x.strip()
    if not x:
        continue
    if x.startswith('$ '):
        cmds.append(x[2:])
        outputs.append('')
    else:
        outputs[-1] = outputs[-1] + x + '\n'

assert len(cmds) == len(outputs)
for cmd, expect in zip(cmds, outputs):
    out = subprocess.check_output(shlex.split(cmd)).decode('utf-8')
    assert out == expect, f'cmd:{cmd} out:{out} expect:{expect}'

print("all tests passed")
