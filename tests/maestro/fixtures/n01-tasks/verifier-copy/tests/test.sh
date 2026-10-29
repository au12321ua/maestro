#!/bin/bash
# The agent's answer arrives as an artifact; the verifier's own files tell where it runs.
echo "verifying in $(hostname): $(cat /verifier-mark 2>/dev/null || echo copy of the main image)"
if [ -f /app/answer ]; then echo 1 > /logs/verifier/reward.txt; else echo 0 > /logs/verifier/reward.txt; fi
