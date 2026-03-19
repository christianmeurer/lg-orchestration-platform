import subprocess


def deploy_to_production(image_tag: str) -> int:
    result = subprocess.run(
        ["kubectl", "set", "image", f"deployment/app=app:{image_tag}"],
        capture_output=True,
    )
    return result.returncode
