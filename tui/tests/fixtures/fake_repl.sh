#!/bin/sh
# Stub interactive harness for seed tests: print a banner (so the TUI's
# readiness wait fires), then echo everything typed into it back out — so the
# seeded first message lands on the agent's vt100 screen where the test can
# assert on it.
printf 'READY>\n'
exec cat
