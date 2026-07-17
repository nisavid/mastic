#!/bin/sh

set -e

# MASTIC adopted Conventional Commits after its preserved legacy histories.
cog check fcdc513aca6734d0abaef2bda2b84ede5f729480..HEAD
