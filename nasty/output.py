GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
RESET  = "\033[0m"
BOLD   = "\033[1m"


def info(msg):   print(f"{CYAN}→{RESET} {msg}")
def ok(msg):     print(f"  {GREEN}✓{RESET} {msg}")
def fail(msg):   print(f"  {RED}✗{RESET} {msg}")
def warn(msg):   print(f"  {YELLOW}!{RESET} {msg}")


def header(msg):
    print(f"\n{BOLD}{'═' * 60}")
    print(f"  {msg}")
    print(f"{'═' * 60}{RESET}\n")
