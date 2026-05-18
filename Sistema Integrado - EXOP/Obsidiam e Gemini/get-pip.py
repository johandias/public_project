# get-pip.py bootstrap script
# Obtido de https://bootstrap.pypa.io/get-pip.py
import sys
import os
import tempfile
import urllib.request

def main():
    url = "https://bootstrap.pypa.io/get-pip.py"
    print("Baixando o instalador oficial do pip...")
    with urllib.request.urlopen(url) as response:
        data = response.read()
    temp = tempfile.NamedTemporaryFile(delete=False, suffix=".py")
    temp.write(data)
    temp.close()
    print(f"Executando o instalador baixado: {temp.name}")
    os.execv(sys.executable, [sys.executable, temp.name] + sys.argv[1:])

if __name__ == "__main__":
    main()
