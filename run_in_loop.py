import traceback
import subprocess
import sys
from time import sleep
from datetime import datetime

from config_reader import config
from engine.log import get_logger


log = get_logger()


def _inside_running_interval() -> bool:
    """Check if the current time is in the running interval

    :rtype: bool
    :returns: Whether the current time is in the running interval or not
    """

    start_time_str = config.behavior.running_interval_start
    end_time_str = config.behavior.running_interval_end

    if start_time_str == "00:00" and end_time_str == "00:00":
        return True

    start_time = datetime.strptime(start_time_str, "%H:%M").time()
    end_time = datetime.strptime(end_time_str, "%H:%M").time()

    if start_time > end_time:
        raise SystemExit("Start time must be before the end time!")

    now = datetime.now().time()

    current_hour = now.hour

    if current_hour == start_time.hour and current_hour == end_time.hour:
        if end_time.minute - start_time.minute < 10:
            raise SystemExit("There should be at least 10 minutes between the start and end!")

    return start_time <= now <= end_time


def main() -> None:

    if getattr(sys, "frozen", False):
        # reinvoke the same EXE and dispatch into run_ad_clicker.main
        command = [sys.executable, "--run-ad-clicker"]
    else:
        # command line mode: run the script like before
        command = [sys.executable, "run_ad_clicker.py"]

    while True:

        if not _inside_running_interval():
            start_time = config.behavior.running_interval_start
            log.info(
                "scheduler",
                "Outside of the running interval. Waiting to start...",
                fields={"start_time": start_time},
            )
            sleep(60)
            continue

        subprocess.run(command)

        log.info(
            "scheduler",
            "Sleeping...",
            fields={"seconds": config.behavior.loop_wait_time},
        )
        sleep(config.behavior.loop_wait_time)


if __name__ == "__main__":

    try:
        main()
    except Exception as exp:
        log.error("scheduler", "Exception occurred. See the details in the log file.")

        message = str(exp).split("\n")[0]
        log.debug("scheduler", "Exception", fields={"error": message})
        details = traceback.format_tb(exp.__traceback__)
        log.debug(
            "scheduler",
            "Exception details:",
            fields={"traceback": "".join(details)},
        )
