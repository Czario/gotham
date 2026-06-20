"""
Progress bar utilities for the data normalization service.
"""
import sys
import time
from typing import Optional, Any, Iterator
from contextlib import contextmanager

try:
    from tqdm import tqdm
    TQDM_AVAILABLE = True
except ImportError:
    TQDM_AVAILABLE = False


class SimpleProgressBar:
    """Simple progress bar implementation when tqdm is not available."""
    
    def __init__(self, total: Optional[int] = None, desc: str = "", 
                 disable: bool = False, file=None):
        self.total = total
        self.desc = desc
        self.disable = disable
        self.file = file or sys.stderr
        self.current = 0
        self.start_time = time.time()
        self.last_print_time = 0
        self.last_print_n = 0
        
    def update(self, n: int = 1):
        """Update progress by n steps."""
        if self.disable:
            return
            
        self.current += n
        current_time = time.time()
        
        # Only update display every 0.1 seconds to avoid spam
        if current_time - self.last_print_time > 0.1 or self.current == self.total:
            self._display()
            self.last_print_time = current_time
            self.last_print_n = self.current
    
    def _display(self):
        """Display the current progress."""
        if self.total:
            percentage = (self.current / self.total) * 100
            elapsed = time.time() - self.start_time
            
            if self.current > 0:
                eta = (elapsed / self.current) * (self.total - self.current)
                eta_str = f", ETA: {int(eta)}s" if eta > 1 else ""
            else:
                eta_str = ""
            
            progress_str = f"\r{self.desc}: {self.current}/{self.total} ({percentage:.1f}%){eta_str}"
        else:
            progress_str = f"\r{self.desc}: {self.current}"
        
        self.file.write(progress_str)
        self.file.flush()
    
    def close(self):
        """Finish the progress bar."""
        if not self.disable:
            self.file.write("\n")
            self.file.flush()
    
    def __enter__(self):
        return self
    
    def __exit__(self, *args):
        self.close()


def create_progress_bar(total: Optional[int] = None, 
                       desc: str = "",
                       disable: bool = False,
                       **kwargs) -> Any:
    """
    Create a progress bar using tqdm if available, fallback to simple implementation.
    
    Args:
        total: Total number of iterations
        desc: Description to show
        disable: Whether to disable the progress bar
        **kwargs: Additional arguments passed to tqdm
    
    Returns:
        Progress bar instance (tqdm or SimpleProgressBar)
    """
    if disable:
        return SimpleProgressBar(total=total, desc=desc, disable=True)
    
    if TQDM_AVAILABLE:
        # Configure tqdm for better display
        tqdm_kwargs = {
            'total': total,
            'desc': desc,
            'unit': 'items',
            'unit_scale': True,
            'dynamic_ncols': True,
            'ascii': True,
            'file': sys.stderr,
            **kwargs
        }
        return tqdm(**tqdm_kwargs)
    else:
        return SimpleProgressBar(total=total, desc=desc, disable=False)


@contextmanager
def progress_bar(iterable=None, total: Optional[int] = None, 
                desc: str = "", disable: bool = False, **kwargs):
    """
    Context manager for progress bars.
    
    Args:
        iterable: Iterable to wrap (if provided)
        total: Total number of iterations
        desc: Description to show
        disable: Whether to disable the progress bar
        **kwargs: Additional arguments
    
    Yields:
        Progress bar instance or wrapped iterable
    """
    if iterable is not None:
        if hasattr(iterable, '__len__') and total is None:
            total = len(iterable)
    
    pbar = create_progress_bar(total=total, desc=desc, disable=disable, **kwargs)
    
    try:
        if iterable is not None:
            if TQDM_AVAILABLE:
                yield pbar
                for item in iterable:
                    yield item
                    pbar.update(1)
            else:
                yield pbar
                for i, item in enumerate(iterable):
                    yield item
                    pbar.update(1)
        else:
            yield pbar
    finally:
        pbar.close()


def progress_wrapper(iterable, desc: str = "", disable: bool = False, **kwargs):
    """
    Wrap an iterable with a progress bar.
    
    Args:
        iterable: Iterable to wrap
        desc: Description to show
        disable: Whether to disable the progress bar
        **kwargs: Additional arguments
    
    Returns:
        Iterator with progress bar
    """
    total = len(iterable) if hasattr(iterable, '__len__') else None
    
    if disable:
        return iterable
    
    if TQDM_AVAILABLE:
        return tqdm(iterable, desc=desc, total=total, 
                   unit='items', unit_scale=True, dynamic_ncols=True,
                   ascii=True, file=sys.stderr, **kwargs)
    else:
        def _wrapped_iterator():
            pbar = SimpleProgressBar(total=total, desc=desc, disable=False)
            try:
                for item in iterable:
                    yield item
                    pbar.update(1)
            finally:
                pbar.close()
        
        return _wrapped_iterator()


def format_time(seconds: float) -> str:
    """Format seconds into a human-readable time string."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    elif seconds < 3600:
        minutes = int(seconds // 60)
        secs = int(seconds % 60)
        return f"{minutes}m {secs}s"
    else:
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        return f"{hours}h {minutes}m"


def format_rate(count: int, elapsed: float) -> str:
    """Format processing rate."""
    if elapsed <= 0:
        return "0 items/s"
    
    rate = count / elapsed
    if rate >= 1:
        return f"{rate:.1f} items/s"
    else:
        return f"{60/rate:.1f} items/min" if rate > 0 else "0 items/s"
