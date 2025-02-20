import os
import base64

yaml="""
apiVersion: v1
data:
  ip: MTAuMTAwLjQuNzE=
  password: UEBzc3cwcmQ=
  username: cW5hcHVzZXI=
kind: Secret
metadata:
  name: qnap
"""

os.system("kubectl get profile > /tmp/users.txt")

f=open("/tmp/users.txt","r")
lines = f.read().split("\n")
for l in lines[1:]:
    ff = l.split(" ")
    u = ff[0]
    cmd = yaml+"\n  namespace: "+u+"\n"
    f=open("/tmp/tmp.yaml","w")
    f.write(yaml)
    f.write("\n  namespace: "+u+"\n")
    f.close()
    print("add qnap to "+u+"\n")
    os.system("kubectl apply -f /tmp/tmp.yaml")
    


