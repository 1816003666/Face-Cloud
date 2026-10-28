
import paramiko
for u,p in [('root','firefly'),('firefly','firefly'),('root','root')]:
    try:
        cc=paramiko.SSHClient(); cc.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        cc.connect('192.168.99.101',port=22,username=u,password=p,timeout=12,banner_timeout=12,auth_timeout=12)
        _,o,e=cc.exec_command('echo OK; uname -a',timeout=10)
        print(u,p,'OK',o.read().decode())
        cc.close(); break
    except Exception as ex:
        print(u,p,'FAIL',str(ex)[:120])
