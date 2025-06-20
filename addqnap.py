import os
import base64

f=open('env.ini')
ip = ''
username=''
password=''
for l in f.read().split("\n"):
    ff=l.split('=')
    if ff[0] == 'QNAP_IP':
        ip = ff[1].strip()
    elif ff[0] == 'QNAP_USERNAME':
        username = ff[1].strip()
    elif ff[0] == 'QNAP_PASSWORD':
        password = ff[1].strip()

if ip =='' or username=='' or password=='':
    print("QNAP configuration is not defined")
    os.exit(-1)

ip = base64.b64encode(ip.encode('UTF-8')).decode('UTF-8')
username = base64.b64encode(username.encode('UTF-8')).decode('UTF-8')
password = base64.b64encode(password.encode('UTF-8')).decode('UTF-8')

os.system("kubectl get profile > /tmp/users.txt")

f=open("/tmp/users.txt","r")
lines = f.read().split("\n")
for l in lines[1:]:
    ff = l.split(" ")
    namespace = ff[0]
    if namespace == '': break
    yaml=f"""
apiVersion: v1
data:
  ip: {ip}
  password: {password}
  username: {username}
kind: Secret
metadata:
  name: qnap
  namespace: {namespace}
---
apiVersion: v1
kind: LimitRange
metadata:
  name: default-cpu-mem-limits
  namespace: {namespace}
spec:
  limits:
  - type: Container
    defaultRequest:
      cpu: "250m"  # 設定預設 CPU requests
      memory: "512Mi"  # 設定預設 Memory requests
    default:
      cpu: "500m"  # 設定預設 CPU limits
      memory: "1Gi"  # 設定預設 Memory limits

"""
    f=open("/tmp/tmp.yaml","w")
    f.write(yaml)
    #f.write("\n  namespace: "+u+"\n")
    f.close()
    print("add qnap to "+namespace+"\n")
    #print(yaml)
    #os.exit(0)
    os.system("kubectl apply -f /tmp/tmp.yaml")
    


