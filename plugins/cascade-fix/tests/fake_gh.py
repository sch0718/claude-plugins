import json
import os
import sys

if "graphql" in sys.argv[1:]:
    with open(os.environ["CASCADE_FAKE_GH_FILE"], encoding="utf-8") as f:
        sys.stdout.write(f.read())
    sys.exit(0)
if sys.argv[1:3] == ["pr", "view"] and "number,url,state" in sys.argv and os.environ.get("CASCADE_FAKE_GH_PR_URL"):
    url = os.environ["CASCADE_FAKE_GH_PR_URL"]
    sys.stdout.write(json.dumps({"number": int(url.rsplit("/", 1)[1]), "url": url, "state": "OPEN"}) + "\n")
    sys.exit(0)
if sys.argv[1:3] == ["api", "user"]:
    sys.stdout.write("me\n")
    sys.exit(0)
if sys.argv[1:3] == ["pr", "view"] and os.environ.get("CASCADE_FAKE_GH_HEAD"):
    sys.stdout.write(os.environ["CASCADE_FAKE_GH_HEAD"] + "\n")
    sys.exit(0)
sys.exit(1)
