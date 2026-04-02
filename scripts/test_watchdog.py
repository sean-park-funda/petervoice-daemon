import sys
import os

# Add scripts directory to path
sys.path.append(os.path.expanduser("~/Projects/peter-voice/scripts"))

import watchdog

print("--- Testing Analyze ---")
watchdog.execute_recovery_command("analyze")
if watchdog.LAST_ANALYSIS_REPORT:
    print("Analyze Report Generated:")
    print(watchdog.LAST_ANALYSIS_REPORT)
else:
    print("Analyze failed to generate report.")

print("\n--- Testing Compact ---")
# This might fail if openclaw is not runnable or needs a session, but let's see the output
watchdog.execute_recovery_command("compact")
print(f"Compact Result: {watchdog.LAST_HEAL_RESULT}")
