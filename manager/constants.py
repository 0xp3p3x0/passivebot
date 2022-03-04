import sys
import os


def get_python_executable():
    if sys.version_info[0] == 3:
        return 'python3'
    else:
        return None


PYTHON_EXC_ALIAS = get_python_executable()
if PYTHON_EXC_ALIAS is None:
    print('Python 3 is not installed on this system')
    sys.exit(1)


MANAGER_PATH = os.path.dirname(os.path.abspath(__file__))
MANAGER_CONFIG_PATH = os.path.join(MANAGER_PATH, 'config.yaml')
PASSIVBOT_PATH = os.getcwd()
s = PASSIVBOT_PATH.split('/')

# support for /home/username and /root paths
UNELEVATED_USER = s[2] if len(s) > 2 else s[1]

# relative to passivbot.py
CONFIGS_PATH = os.path.join(PASSIVBOT_PATH, 'configs/live')
SERVICES_PATH = '/etc/systemd/system'

INSTANCE_SIGNATURE_BASE = [PYTHON_EXC_ALIAS, '-u', 'passivbot.py']
