# Spike-sorting-INTAN-to-PDF
Spike sorting GUI with spikeinterface 103.2 -  Output a pdf files summarizing the analysis
Not the spikeinterface native GUI.
You do not need to install spikeinterface beforehand.

**Multi-platform:** Windows, macOS, Linux.

The library is available on PyPI.

## Installation (PyPI)
1. Open terminal as administrator
2. Run on terminal [uv](https://docs.astral.sh/uv/): `curl -LsSf https://astral.sh/uv/install.sh | sh` (macOS/Linux) or `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"` (Windows)
3. Install virtual environment : run in terminal `uv venv si_env --python 3.12`
4. Restart your terminal
5. Allow script execution : run in terminal `Set-ExecutionPolicy -ExecutionPolicy RemoteSigned`
6. Activate virtual environment: run in terminal `source si_env/bin/activate` (macOS/Linux) or `si_env\Scripts\activate` (Windows)
7. Install library : run in terminal `uv pip install spike-sorting-intan-to-pdf`

## Run application
1. Allow script execution : run in terminal `Set-ExecutionPolicy -ExecutionPolicy RemoteSigned`
2. Activate virtual environment: run in terminal `source si_env/bin/activate` (macOS/Linux) or `si_env\Scripts\activate` (Windows)
3. Run in terminal `spike-sorting-intan-to-pdf`
