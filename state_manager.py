# state_manager.py
import os
import logging
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
from typing import Optional

from settings import AppSettings, EASTERN_TZ # Import shared settings and constants

class StateManager:
    def __init__(self, settings: AppSettings):
        self.settings = settings

    def get_last_api_check_time(self) -> datetime:
        """Reads the last API check date from file or returns a default."""
        if self.settings.last_date_override:
            logging.info(f"Using override date for API check: {self.settings.last_date_override}")
            return self.settings.last_date_override

        file_path = self.settings.last_submission_file
        if os.path.exists(file_path):
            try:
                with open(file_path, 'r') as f:
                    date_str = f.read().strip()
                    dt = datetime.fromisoformat(date_str)
                    logging.info(f"Read last API check date from file: {dt}")
                    return dt
            except Exception as e:
                logging.error(f"Error reading last API check date from {file_path}: {e}. Using default (yesterday).")
                return self._default_past_date()
        else:
            logging.warning(f"{file_path} not found. Using default last check date (yesterday).")
            # Optionally create the file with the default date here if desired
            # self.save_last_api_check_time(self._default_past_date())
            return self._default_past_date()

    def save_last_api_check_time(self, time: datetime):
        """Saves the API check time to file."""
        if self.settings.no_save:
            logging.info("Skipping save of last API check time (--nosave).")
            return

        file_path = self.settings.last_submission_file
        try:
            # Add a small delta to avoid reprocessing the exact same timestamp
            save_time = time + timedelta(seconds=1)
            with open(file_path, 'w') as f:
                f.write(save_time.isoformat())
            logging.info(f"Saved last API check time {save_time.isoformat()} to {file_path}")
        except Exception as e:
            logging.error(f"Error saving last API check time to {file_path}: {e}")

    def _get_last_rss_check_date_from_file(self) -> Optional[date]:
        """Reads the last RSS check date from its file."""
        file_path = self.settings.last_rss_check_file
        if os.path.exists(file_path):
            try:
                with open(file_path, 'r') as f:
                    date_str = f.read().strip()
                    return datetime.fromisoformat(date_str).date()
            except Exception as e:
                logging.error(f"Error reading last RSS check date from {file_path}: {e}")
        return None # Indicate file not found or error

    def has_checked_rss_today(self) -> bool:
        """Checks if the RSS feed was already checked today (ET)."""
        if self.settings.force_rss_check:
            logging.info("Forcing RSS check (--forcerss).")
            return False

        last_check_date = self._get_last_rss_check_date_from_file()
        if last_check_date is None:
             logging.info(f"{self.settings.last_rss_check_file} not found or unreadable. Assuming RSS not checked today.")
             return False # Treat as not checked if file missing/error

        current_date_et = datetime.now(EASTERN_TZ).date()
        has_checked = last_check_date == current_date_et
        if has_checked:
            logging.info(f"RSS feed already checked today ({current_date_et}).")
        else:
            logging.info(f"RSS feed not yet checked today ({current_date_et}). Last check was {last_check_date}.")
        return has_checked

    def save_rss_check_time(self):
        """Saves the current time (ET) as the last RSS check time."""
        if self.settings.no_save:
            logging.info("Skipping save of RSS check time (--nosave).")
            return

        file_path = self.settings.last_rss_check_file
        try:
            now_et = datetime.now(EASTERN_TZ)
            with open(file_path, 'w') as f:
                f.write(now_et.isoformat())
            logging.info(f"Saved current RSS check time {now_et.isoformat()} to {file_path}")
        except Exception as e:
            logging.error(f"Error saving RSS check time to {file_path}: {e}")

    def _default_past_date(self) -> datetime:
        """Returns a datetime object for yesterday."""
        # Use timezone-naive datetime for comparison with arXiv's naive datetimes
        return datetime.now() - timedelta(days=1)