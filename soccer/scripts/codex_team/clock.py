#!/usr/bin/env python3
import json
from tc import request

print(json.dumps(request('clock'), ensure_ascii=False, indent=2))
