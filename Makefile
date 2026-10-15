# Convenience wrapper around the Vulcan Docker setup
# (Vulcan book: guides/get-started/docker/).
#
# The book defines a shell alias:
#   alias vulcan="docker run -it --network=vulcan --rm -v .:/workspace tmdcio/vulcan-postgres:0.228.1.19 vulcan"
# This Makefile does the same, and also passes your .env file into the
# container so the env_var(...) lookups in config.yaml resolve.
#
# Quick start:
#   cp .env.example .env
#   make network up data SEED=<your seed> info
#   make plan-dev run-dev
#   make grade

VULCAN_IMAGE ?= tmdcio/vulcan-postgres:0.228.1.19
ENV_FILE := $(if $(wildcard .env),--env-file .env,)
DOCKER_RUN = docker run --rm --network=vulcan $(ENV_FILE) -e PYTHONDONTWRITEBYTECODE=1 -v "$(CURDIR)":/workspace -w /workspace
VULCAN ?= $(DOCKER_RUN) -it $(VULCAN_IMAGE) vulcan
# VERIFY: the image ships python3 (Vulcan is a Python application) and lets us override the
#         entrypoint with --entrypoint python3.
PYTHON_IN_IMAGE = $(DOCKER_RUN) --entrypoint python3 $(VULCAN_IMAGE)
ENV ?= dev

# Git facts are computed here, on the host, because git may not exist in the image.
GRADE_COMMIT = $(shell git rev-parse HEAD 2>/dev/null)
GRADE_DIRTY = $(shell git rev-parse --git-dir >/dev/null 2>&1 && { test -z "$$(git status --porcelain 2>/dev/null)" && echo false || echo true; })
GRADE_TAGS = $(shell git tag --points-at HEAD 2>/dev/null | tr '\n' ' ')

.PHONY: help network up down info lint format format-check test audit plan-dev run-dev data grade grade-dry-run grader-checksum

help:
	@echo "make network        create the 'vulcan' docker network (once)"
	@echo "make up / down      start / stop warehouse + statestore (docker-compose.infra.yml)"
	@echo "make data SEED=n    regenerate seeds/*.csv and seeds/SEED with your assigned seed"
	@echo "make info           vulcan info"
	@echo "make lint           vulcan lint"
	@echo "make format         vulcan format (rewrites files)"
	@echo "make format-check   vulcan format --check (what the grader runs)"
	@echo "make test           vulcan test"
	@echo "make plan-dev       vulcan plan dev"
	@echo "make run-dev        vulcan run dev"
	@echo "make audit          vulcan audit"
	@echo "make grade          run the grader -> grading-report/report.md + report.json"
	@echo "make grade-dry-run  structure and static checks only (no Vulcan, no database)"
	@echo "make grader-checksum  print the grader checksum (needs python3 on the host)"

network:
	-docker network create vulcan

up:
	docker compose -f docker-compose.infra.yml up -d

down:
	docker compose -f docker-compose.infra.yml down

info:
	$(VULCAN) info

lint:
	$(VULCAN) lint

format:
	$(VULCAN) format

format-check:
	$(VULCAN) format --check

test:
	$(VULCAN) test

audit:
	$(VULCAN) audit

plan-dev:
	$(VULCAN) plan $(ENV)

run-dev:
	$(VULCAN) run $(ENV)

data:
	@test -n "$(SEED)" || { echo "usage: make data SEED=<your assigned seed>"; exit 2; }
	$(PYTHON_IN_IMAGE) tools/generate_data.py --seed $(SEED)

# Grades into a separate, freshly re-created database (vulcan_grading) on your local
# warehouse server, environment `grading`. Your repo, dev environment and state are not touched.
grade:
	@mkdir -p grading-report
	$(DOCKER_RUN) \
	  -e VULCAN_CMD=vulcan \
	  -e GRADER_PG_HOST=warehouse -e GRADER_PG_PORT=5432 \
	  -e GRADER_PG_DB=vulcan_grading -e GRADER_PG_ADMIN_DB=warehouse \
	  -e GRADE_COMMIT="$(GRADE_COMMIT)" -e GRADE_DIRTY="$(GRADE_DIRTY)" -e GRADE_TAGS="$(GRADE_TAGS)" \
	  --entrypoint python3 $(VULCAN_IMAGE) grader/grade.py --submission /workspace --out /workspace/grading-report
	@echo "Read grading-report/report.md, then upload grading-report/report.json on the course page."

grade-dry-run:
	@mkdir -p grading-report
	$(DOCKER_RUN) \
	  -e GRADE_COMMIT="$(GRADE_COMMIT)" -e GRADE_DIRTY="$(GRADE_DIRTY)" -e GRADE_TAGS="$(GRADE_TAGS)" \
	  --entrypoint python3 $(VULCAN_IMAGE) grader/grade.py --submission /workspace --out /workspace/grading-report --dry-run

grader-checksum:
	@python3 grader/grade.py --print-checksum
