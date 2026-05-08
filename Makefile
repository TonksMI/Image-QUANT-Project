.PHONY: help install test test-unit test-integration coverage lint reproduce dashboard

PYTHON  := python
PYTEST  := pytest
COVERAGE:= pytest --cov=src/urbangrowth --cov-report=term-missing --cov-report=html
STREAMLIT := streamlit

help:
	@echo "Urban Growth Research Platform — make targets"
	@echo ""
	@echo "  install          Create conda env and install package in editable mode"
	@echo "  test             Run full test suite"
	@echo "  test-unit        Run unit tests only (no DB required)"
	@echo "  test-integration Run integration tests (no DB required)"
	@echo "  coverage         Run tests with coverage report (target: >70%)"
	@echo "  lint             Run ruff linter"
	@echo "  reproduce        Full pipeline end-to-end on 2022-01 → 2022-06 sample"
	@echo "  dashboard        Launch Streamlit dashboard"

install:
	conda env create -f environment.yml --name urbangrowth || true
	conda run -n urbangrowth pip install --pre torch torchvision \
	    --index-url https://download.pytorch.org/whl/nightly/cu128
	conda run -n urbangrowth pip install -e ".[dev]"

test:
	$(PYTEST) tests/ -v

test-unit:
	$(PYTEST) tests/unit/ -v

test-integration:
	$(PYTEST) tests/integration/ -v

coverage:
	$(COVERAGE) tests/
	@echo ""
	@echo "HTML report: htmlcov/index.html"

lint:
	ruff check src/ tests/

reproduce:
	$(PYTHON) scripts/reproduce.py \
	    --start 2022-01 \
	    --end   2022-06 \
	    --model ridge \
	    --horizon 1

dashboard:
	$(STREAMLIT) run dashboards/streamlit_app.py
