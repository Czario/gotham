#!/usr/bin/env python3
"""
Unified Test Runner for SEC Data Scraper v9

Usage:
    python run_tests.py unit          # Run only unit tests
    python run_tests.py integration   # Run only integration tests  
    python run_tests.py e2e           # Run only end-to-end tests
    python run_tests.py all           # Run all tests
    python run_tests.py fast          # Run fast tests (unit + integration)
    python run_tests.py coverage      # Run with coverage report
    python run_tests.py --help        # Show this help

Examples:
    python run_tests.py unit --verbose
    python run_tests.py integration --debug
    python run_tests.py all --coverage --html
"""

import sys
import os
import argparse
import subprocess
from pathlib import Path
from typing import List, Optional

class TestRunner:
    """Unified test runner for different test suites"""
    
    def __init__(self):
        self.project_root = Path(__file__).parent
        self.venv_python = self.project_root / ".venv" / "bin" / "python"
        self.use_uv = (self.project_root / "uv.lock").exists()
        
        # Test suite configurations
        self.test_suites = {
            'unit': {
                'path': 'tests/unit/',
                'description': 'Unit tests - fast isolated tests',
                'markers': [],
                'parallel': True
            },
            'integration': {
                'path': 'tests/integration/', 
                'description': 'Integration tests - component interaction tests',
                'markers': ['integration'],
                'parallel': False
            },
            'e2e': {
                'path': 'tests/e2e/',
                'description': 'End-to-end tests - full workflow tests', 
                'markers': ['e2e'],
                'parallel': False
            }
        }
        
    def build_pytest_command(self, suite: str, args: argparse.Namespace) -> List[str]:
        """Build pytest command based on suite and arguments"""
        if self.use_uv:
            cmd = ["uv", "run", "pytest"]
        else:
            cmd = [str(self.venv_python), "-m", "pytest"]
        
        # Add test path
        if suite in self.test_suites:
            test_path = self.test_suites[suite]['path']
            if os.path.exists(test_path):
                cmd.append(test_path)
            else:
                print(f"⚠️  Warning: {test_path} directory not found")
        
        # Add markers if specified
        if suite in self.test_suites and self.test_suites[suite]['markers']:
            for marker in self.test_suites[suite]['markers']:
                cmd.extend(["-m", marker])
        
        # Add verbosity
        if args.verbose:
            cmd.append("-v")
        elif args.quiet:
            cmd.append("-q")
            
        # Add parallel execution for unit tests
        if suite == 'unit' and self.test_suites[suite]['parallel'] and not args.no_parallel:
            try:
                import pytest_xdist
                cmd.extend(["-n", "auto"])
            except ImportError:
                print("📝 Note: Install pytest-xdist for parallel test execution")
        
        # Add coverage
        if args.coverage:
            cmd.extend([
                "--cov=core",
                "--cov=database",
                "--cov=utilities",
                "--cov=api",
                "--cov=normalization/src/data_normalization_service",
                "--cov-report=term-missing"
            ])
            
            if args.html:
                cmd.extend(["--cov-report=html:htmlcov"])
                
        # Add extra pytest arguments
        if args.pytest_args:
            cmd.extend(args.pytest_args)
            
        # Stop on first failure if requested
        if args.fail_fast:
            cmd.append("-x")
            
        # Add debug output
        if args.debug:
            cmd.extend(["--tb=long", "--capture=no"])
            
        return cmd
    
    def run_suite(self, suite: str, args: argparse.Namespace) -> bool:
        """Run a specific test suite"""
        print(f"\n🧪 Running {suite} tests...")
        
        if suite in self.test_suites:
            print(f"📋 {self.test_suites[suite]['description']}")
        
        cmd = self.build_pytest_command(suite, args)
        
        if args.dry_run:
            print(f"🔍 Would run: {' '.join(cmd)}")
            return True
            
        print(f"▶️  Command: {' '.join(cmd[2:])}")  # Skip python -m
        print("─" * 50)
        
        try:
            result = subprocess.run(cmd, cwd=self.project_root)
            success = result.returncode == 0
            
            if success:
                print(f"✅ {suite} tests PASSED")
            else:
                print(f"❌ {suite} tests FAILED")
                
            return success
            
        except KeyboardInterrupt:
            print(f"\n⚠️  {suite} tests interrupted by user")
            return False
        except Exception as e:
            print(f"❌ Error running {suite} tests: {e}")
            return False
    
    def run_multiple_suites(self, suites: List[str], args: argparse.Namespace) -> bool:
        """Run multiple test suites"""
        results = {}
        overall_success = True
        
        print(f"\n🚀 Running {len(suites)} test suites: {', '.join(suites)}")
        
        for suite in suites:
            success = self.run_suite(suite, args)
            results[suite] = success
            if not success:
                overall_success = False
                
            if not success and args.fail_fast:
                print(f"\n🛑 Stopping due to {suite} test failures (--fail-fast)")
                break
                
        # Print summary
        print("\n" + "="*50)
        print("📊 TEST SUMMARY")
        print("="*50)
        
        for suite, success in results.items():
            status = "✅ PASSED" if success else "❌ FAILED"
            print(f"{suite:12} {status}")
            
        print("─" * 50)
        if overall_success:
            print("🎉 ALL TESTS PASSED!")
        else:
            print("💥 SOME TESTS FAILED!")
            
        return overall_success

def create_parser() -> argparse.ArgumentParser:
    """Create argument parser"""
    parser = argparse.ArgumentParser(
        description="Unified Test Runner for SEC Data Scraper v9",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    
    parser.add_argument(
        'suite',
        choices=['unit', 'integration', 'e2e', 'all', 'fast', 'coverage'],
        help='Test suite to run'
    )
    
    parser.add_argument(
        '-v', '--verbose',
        action='store_true',
        help='Verbose output'
    )
    
    parser.add_argument(
        '-q', '--quiet', 
        action='store_true',
        help='Quiet output'
    )
    
    parser.add_argument(
        '-x', '--fail-fast',
        action='store_true', 
        help='Stop on first failure'
    )
    
    parser.add_argument(
        '--coverage',
        action='store_true',
        help='Run with coverage report'
    )
    
    parser.add_argument(
        '--html',
        action='store_true',
        help='Generate HTML coverage report (requires --coverage)'
    )
    
    parser.add_argument(
        '--debug',
        action='store_true',
        help='Enable debug mode with detailed output'
    )
    
    parser.add_argument(
        '--no-parallel',
        action='store_true',
        help='Disable parallel test execution'
    )
    
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Show commands that would be run without executing'
    )
    
    parser.add_argument(
        'pytest_args',
        nargs='*',
        help='Additional arguments to pass to pytest'
    )
    
    return parser

def main():
    """Main entry point"""
    parser = create_parser()
    args = parser.parse_args()
    
    runner = TestRunner()
    
    # Handle special suite combinations
    if args.suite == 'all':
        suites = ['unit', 'integration', 'e2e']
    elif args.suite == 'fast':
        suites = ['unit', 'integration'] 
    elif args.suite == 'coverage':
        args.coverage = True
        suites = ['unit', 'integration']
    else:
        suites = [args.suite]
    
    # Validate virtual environment
    if not runner.venv_python.exists():
        print("❌ Virtual environment not found at .venv/bin/python")
        print("🔧 Please run: python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt")
        sys.exit(1)
    
    # Run tests
    if len(suites) == 1:
        success = runner.run_suite(suites[0], args)
    else:
        success = runner.run_multiple_suites(suites, args)
    
    # Exit with appropriate code
    sys.exit(0 if success else 1)

if __name__ == "__main__":
    main()
