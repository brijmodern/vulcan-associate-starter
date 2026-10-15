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
#   make network up info
#   make plan-dev run-dev

VULCAN_IMAGE ?= tmdcio/vulcan-postgres:0.228.1.19
VULCAN ?= docker run -it --rm --network=vulcan --env-file .env -v "$(CURDIR)":/workspace -w /workspace $(VULCAN_IMAGE) vulcan
ENV ?= dev

.PHONY: help network up down info lint format format-check test render audit plan-dev run-dev data

help:
	@echo "make network       create the 'vulcan' docker network (once)"
	@echo "make up / down     start / stop warehouse + statestore (docker-compose.infra.yml)"
	@echo "make info          vulcan info"
	@echo "make lint          vulcan lint"
	@echo "make format        vulcan format (rewrites files)"
	@echo "make format-check  vulcan format --check (what the grader runs)"
	@echo "make test          vulcan test"
	@echo "make plan-dev      vulcan plan dev"
	@echo "make run-dev       vulcan run dev"
	@echo "make audit         vulcan audit"
	@echo "make data SEED=n   regenerate seeds/*.csv with your assigned seed"

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
	python3 tools/generate_data.py --seed $(SEED)
