\# Cassian – Kindroid Discord Bot



This is the private deployment of \*\*Cassian\*\*, a fully modular AI personality powered by \[Kindroid](https://kindroid.ai) and integrated with Discord via Python.



\## Features



\- Kindroid API integration

\- Discord bot with memory and style overlays

\- Configurable tone, backstory, and behavior

\- File-based modular AI memory system



\## Repo Structure



\- `Bot/` — main runtime script (`cassian.py`)

\- `Security/` — contains `.env` with API keys (\*\*ignored from Git\*\*)

\- `Config/` — system-level config (`config.json`)

\- `Key Memories/`, `Response Directives/`, `Backstory/` — text-based memory overlays



\## Usage



1\. Clone the repo

2\. Add your own `.env` file in `Security/`

3\. Edit `config.json` to point to your memory files

4\. Run with `python Bot/cassian.py`



