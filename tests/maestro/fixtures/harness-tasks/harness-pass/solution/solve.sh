#!/bin/bash
# Reference solution; also records whether the agent phase could reach the internet.
echo ok > /app/answer
net-probe > /logs/agent/probe.txt
