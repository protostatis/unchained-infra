from setuptools import find_packages, setup


setup(
    name="unchained-pyreplab",
    version="0.1.0",
    description="Local toy V1 for browser-to-lab workflows with Unchained MCP and pyreplab",
    long_description=(open("README.md", encoding="utf-8").read()),
    long_description_content_type="text/markdown",
    python_requires=">=3.9",
    packages=find_packages(include=["unchained_pyreplab*"]),
    install_requires=["pandas>=2.2,<3"],
    entry_points={
        "console_scripts": [
            "unchained-pyreplab=unchained_pyreplab.cli:main",
            "unchained-pyreplab-codex-agent=unchained_pyreplab.codex_adapter:main",
        ]
    },
)
