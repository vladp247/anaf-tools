"""ANAF Intelligence Platform — Configuration."""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()
BASE_DIR = Path(__file__).parent


class Config:
    # Server
    HOST: str = os.getenv("HOST", "127.0.0.1")
    PORT: int = int(os.getenv("PORT", "8745"))
    DEBUG: bool = os.getenv("DEBUG", "false").lower() == "true"

    # ANAF API
    ANAF_COMPANY_URL: str = "https://webservicesp.anaf.ro/api/PlatitorTvaRest/v9/tva"
    ANAF_FINANCIALS_URL: str = "https://webservicesp.anaf.ro/bilant"
    FINANCIALS_MIN_YEAR: int = 2014
    FINANCIALS_MAX_YEAR: int = 2025
    REQUEST_TIMEOUT: float = float(os.getenv("REQUEST_TIMEOUT", "20.0"))
    MAX_RETRIES: int = int(os.getenv("MAX_RETRIES", "2"))
    RETRY_DELAY: float = float(os.getenv("RETRY_DELAY", "2.0"))
    BULK_RATE_DELAY: float = float(os.getenv("BULK_RATE_DELAY", "2.0"))
    COMPANY_BATCH_SIZE: int = 50

    # Logging
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    LOG_DIR: str = "logs"

    # ONRC Open Data
    DATA_DIR: Path = BASE_DIR / "data"
    ONRC_FIRME_PATH: Path = BASE_DIR / "data" / "OD_FIRME.csv"
    ONRC_STARE_PATH: Path = BASE_DIR / "data" / "OD_STARE_FIRMA.csv"
    ONRC_REPS_PATH:  Path = BASE_DIR / "data" / "OD_REPREZENTANTI_LEGALI.csv"
    ONRC_REPS_IF_PATH: Path = BASE_DIR / "data" / "OD_REPREZENTANTI_IF.csv"
    ONRC_DB_PATH:    Path = BASE_DIR / "data" / "onrc.db"
    NOMENCLATOR_PATH: Path = BASE_DIR / "nomenclator.csv"

    # Setup
    CSV_VERSION_PATH: Path = BASE_DIR / "data" / "csv_version.txt"
    SETUP_DONE_PATH:  Path = BASE_DIR / "data" / ".setup_done"

    # CAEN financial data files  (key, filename_template, description, priority)
    CAEN_DB_PATH: Path = BASE_DIR / "data" / "caen.db"
    CAEN_FILE_DEFS: list = [
        ("uu",    "web_uu_an{year}.txt",       "Bilanț prescurtat (micro/small)",  1),
        ("bl_bs", "web_bl_bs_sl_an{year}.txt", "Bilanț lung/scurt (medium/large)", 2),
        ("ir",    "web_ir_an{year}.txt",        "Bilanț IFRS",                      3),
    ]
    CAEN_KNOWN_MISSING: dict = {
        ("ir", 2021): (
            "ANAF does not publish IFRS data for 2021. "
            "Large multinationals and listed companies that file under IFRS "
            "are absent for this year."
        ),
    }

    @classmethod
    def caen_file(cls, key: str, year: int) -> "Path":
        template = next(t for k, t, *_ in cls.CAEN_FILE_DEFS if k == key)
        return cls.DATA_DIR / template.format(year=year)

    # BNR end-of-year reference exchange rates RON/EUR
    EUR_RATES: dict = {
        2014: 4.4828, 2015: 4.5245, 2016: 4.5390, 2017: 4.6597,
        2018: 4.6639, 2019: 4.7452, 2020: 4.8371, 2021: 4.9204,
        2022: 4.9315, 2023: 4.9465, 2024: 4.9746, 2025: 5.0415,
    }
