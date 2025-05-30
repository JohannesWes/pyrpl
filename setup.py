#!/usr/bin/env python3
"""
Modern setup.py for pyrpl package
"""

import os
import sys
import subprocess
from pathlib import Path
from setuptools import setup, find_packages
from setuptools.command.build_py import build_py
from setuptools.command.develop import develop

# Get the directory containing this setup.py file
SETUP_DIR = Path(__file__).parent.absolute()


def read_file(filename):
    """Read a file and return its contents."""
    filepath = SETUP_DIR / filename
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            return f.read()
    except FileNotFoundError:
        return ''


def get_version():
    """Get version from _version.py file."""
    version_file = SETUP_DIR / 'pyrpl' / '_version.py'
    version_locals = {}

    if version_file.exists():
        with open(version_file, 'r', encoding='utf-8') as f:
            exec(f.read(), None, version_locals)
        return version_locals.get('__version__', '0.1.0')
    else:
        return '0.1.0'


def get_long_description():
    """Get long description from README files."""
    # Try README.rst first, then README.md
    readme_rst = read_file('README.rst')
    if readme_rst:
        return readme_rst

    readme_md = read_file('README.md')
    if readme_md:
        return readme_md

    return "DSP servo controller for quantum optics with the RedPitaya"


def get_requirements():
    """Get requirements, handling different environments."""
    base_requirements = [
        'scp',
        'scipy',
        'pyyaml',
        'pandas',
        'pyqtgraph',
        'numpy>=1.16',
        'paramiko>=2.7',
        'qtpy>=1.11',
        'nbconvert',
        'jupyter-client',
    ]

    # Python version specific requirements
    if sys.version_info >= (3, 7):
        base_requirements.append('qasync')

    # Environment specific requirements
    if os.environ.get('READTHEDOCS') == 'True':
        # Minimal requirements for ReadTheDocs
        return ['sphinx', 'sphinx_bootstrap_theme']

    return base_requirements


def run_command(cmd, cwd=None):
    """Run a shell command safely."""
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True
        )
        print(f"Command '{cmd}' executed successfully")
        return result.returncode == 0
    except subprocess.CalledProcessError as e:
        print(f"Command '{cmd}' failed with error: {e}")
        return False


class CompileFPGACommand(build_py):
    """Custom command to compile FPGA code."""

    def run(self):
        """Run FPGA compilation."""
        print("Compiling FPGA code...")
        fpga_dir = SETUP_DIR / 'pyrpl' / 'fpga'

        if fpga_dir.exists():
            if run_command('make', cwd=fpga_dir):
                print("FPGA compilation successful")
            else:
                print("FPGA compilation failed (Vivado may not be installed)")
        else:
            print("FPGA directory not found, skipping compilation")

        # Continue with normal build
        super().run()


class CompileServerCommand(build_py):
    """Custom command to compile server code."""

    def run(self):
        """Run server compilation."""
        print("Compiling server code...")
        server_dir = SETUP_DIR / 'pyrpl' / 'monitor_server'

        if server_dir.exists():
            run_command('make clean', cwd=server_dir)
            if run_command('make', cwd=server_dir):
                print("Server compilation successful")
            else:
                print("Server compilation failed (GCC cross-compiler may not be installed)")
        else:
            print("Server directory not found, skipping compilation")

        # Continue with normal build
        super().run()


class DevelopWithCompilation(develop):
    """Development install with compilation."""

    def run(self):
        """Run development install with compilation."""
        # Compile FPGA and server if requested
        if '--compile-fpga' in sys.argv:
            CompileFPGACommand(self.distribution).run()
            sys.argv.remove('--compile-fpga')

        if '--compile-server' in sys.argv:
            CompileServerCommand(self.distribution).run()
            sys.argv.remove('--compile-server')

        # Continue with normal develop
        super().run()


# Package configuration
setup(
    name='pyrpl',
    version=get_version(),
    description='DSP servo controller for quantum optics with the RedPitaya',
    long_description=get_long_description(),
    long_description_content_type='text/markdown',  # or 'text/x-rst' if using RST

    # Author information
    author='Leonhard Neuhaus',
    author_email='neuhaus@lkb.upmc.fr',
    url='http://lneuhaus.github.io/pyrpl/',

    # License and classifiers
    license='MIT',
    classifiers=[
        'Development Status :: 4 - Beta',
        'Intended Audience :: Science/Research',
        'License :: OSI Approved :: MIT License',
        'Operating System :: OS Independent',
        'Programming Language :: Python :: 3',
        'Programming Language :: Python :: 3.7',
        'Programming Language :: Python :: 3.8',
        'Programming Language :: Python :: 3.9',
        'Programming Language :: Python :: 3.10',
        'Programming Language :: Python :: 3.11',
        'Programming Language :: Python :: 3.12',
        'Programming Language :: C',
        'Topic :: Scientific/Engineering :: Physics',
        'Topic :: Scientific/Engineering :: Human Machine Interfaces',
    ],

    # Keywords
    keywords=[
        'RedPitaya', 'DSP', 'FPGA', 'IIR', 'PDH', 'synchronous detection',
        'filter', 'PID', 'control', 'lockbox', 'servo', 'feedback', 'lock',
        'quantum optics'
    ],

    # Package configuration
    packages=find_packages(include=['pyrpl', 'pyrpl.*']),
    package_data={
        'pyrpl': [
            'fpga/*',
            'fpga/**/*',
            'monitor_server/*',
            'monitor_server/**/*',
            'config/*',
            'config/**/*',
            'widgets/images/*',
        ]
    },
    include_package_data=True,

    # Requirements
    python_requires='>=3.7',
    install_requires=get_requirements(),

    # Optional dependencies
    extras_require={
        'dev': [
            'pytest>=6.0',
            'pytest-cov',
            'black',
            'flake8',
            'mypy',
        ],
        'gui': [
            'matplotlib',
            'PyQt5',  # or PyQt6
        ],
        'docs': [
            'sphinx',
            'sphinx_bootstrap_theme',
            'pandoc',
        ],
    },

    # Testing
    test_suite='pytest',
    tests_require=['pytest>=6.0'],

    # Entry points (if you have command-line scripts)
    # entry_points={
    #     'console_scripts': [
    #         'pyrpl=pyrpl.cli:main',
    #     ],
    # },

    # Custom commands
    cmdclass={
        'build_fpga': CompileFPGACommand,
        'build_server': CompileServerCommand,
        'develop': DevelopWithCompilation,
    },

    # Project URLs
    project_urls={
        'Bug Reports': 'https://github.com/lneuhaus/pyrpl/issues',
        'Source': 'https://github.com/lneuhaus/pyrpl',
        'Documentation': 'http://lneuhaus.github.io/pyrpl/',
    },

    # Zip safety
    zip_safe=False,
)