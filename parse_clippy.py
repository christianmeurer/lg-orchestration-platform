import json
import subprocess
import sys

def main():
    result = subprocess.run(
        ["cargo", "clippy", "--manifest-path", "rs/runner/Cargo.toml", "--all-targets", "--message-format=json", "--", "-D", "warnings", "-W", "clippy::pedantic"],
        capture_output=True,
        text=True,
        cwd="c:/Users/chris/Projects/Lula"
    )
    
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except:
            continue
            
        if msg.get("reason") == "compiler-message":
            message = msg["message"]
            if message["level"] in ("error", "warning"):
                # Print file, line and message
                spans = message.get("spans", [])
                if spans:
                    span = spans[0]
                    file_name = span["file_name"]
                    line_start = span["line_start"]
                    code = message.get("code")
                    code_id = code["code"] if code else "unknown"
                    print(f"{file_name}:{line_start} [{code_id}] {message['message']}")
                    
if __name__ == "__main__":
    main()