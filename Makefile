.PHONY: demo check
PYTHON ?= python
demo:
	$(PYTHON) scripts/demo.py
check:
	ruff check .
	pytest -q
