"""Execute a notebook top-to-bottom with nbclient and save the outputs."""

from __future__ import annotations

import sys
from pathlib import Path

import nbformat
from nbclient import NotebookClient
from jupyter_client import AsyncKernelManager
from jupyter_client.kernelspec import KernelSpec


class CurrentPythonKernelManager(AsyncKernelManager):
    """Launch the kernel with the interpreter running this script."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._kernel_spec = KernelSpec(
            argv=[
                sys.executable,
                "-m",
                "ipykernel_launcher",
                "-f",
                "{connection_file}",
            ],
            display_name=f"Python ({sys.executable})",
            language="python",
        )


def execute_notebook(path: Path) -> None:
    notebook = nbformat.read(path, as_version=4)
    nbformat.validate(notebook)
    client = NotebookClient(
        notebook,
        timeout=180,
        kernel_name="python3",
        kernel_manager_class=CurrentPythonKernelManager,
        resources={"metadata": {"path": str(path.parent)}},
    )
    client.execute()
    nbformat.validate(notebook)
    nbformat.write(notebook, path)


if __name__ == "__main__":
    notebook_path = Path(
        sys.argv[1] if len(sys.argv) > 1 else "notebooks/build_master_400k.ipynb"
    ).resolve()
    execute_notebook(notebook_path)
    print(f"Executed and validated {notebook_path}")
