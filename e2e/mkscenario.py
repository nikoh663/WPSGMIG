"""Write a drive.py script: mkscenario.py NAME CWD 'tool args' STEPS_JSON"""
import json, subprocess, sys
name, cwd, args, steps = sys.argv[1], sys.argv[2], sys.argv[3], json.loads(sys.argv[4])
env = json.loads(subprocess.check_output([sys.executable, "envjson.py"], cwd="/home/user/wpm/e2e"))
argv = ["/usr/sbin/runuser", "-u", "wpuser", "--", "python3", "/opt/wpmigrate/wpmigrate.pyz", *args.split()]
json.dump({"cwd": cwd, "argv": argv, "env": env, "steps": steps, "timeout": 1200}, open(f"scenarios/{name}.json", "w"), indent=1)
