# pi-bake — local build / test / packaging.
#
# Distribution only. Remote deploy + bake is a separate concern;
# see scripts/remote-bake.sh for that workflow.
#
# Variables (override on the command line):
#   PYTHON  — Python interpreter for the build (default: python3)
#
# Common workflows:
#   make test
#   make dist
#   make install     # editable install into the current env

PYTHON ?= python3

.PHONY: help test dist install clean

help: ## Show this help (default target).
	@printf "pi-bake — local build/test/package targets\n\n"
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | sort | \
	  awk 'BEGIN {FS=":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

test: ## Run the unit test suite.
	$(PYTHON) -m pytest -q

dist: ## Build wheel + sdist into ./dist/ (needs `pip install build`).
	rm -rf dist build *.egg-info src/*.egg-info
	$(PYTHON) -m build

install: ## pip install pi-bake into the current environment (editable).
	$(PYTHON) -m pip install -e .

clean: ## Remove build artifacts.
	rm -rf dist build *.egg-info src/*.egg-info
