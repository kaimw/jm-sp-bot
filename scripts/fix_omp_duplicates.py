#!/usr/bin/env python3
"""Fix order_middle_platform.py: remove orphaned BlockerBody body + old duplicate classes."""
import sys

path = sys.argv[1] if len(sys.argv) > 1 else "backend/app/services/order_middle_platform.py"

with open(path) as f:
    lines = f.readlines()

# 1. Remove orphaned BlockerLevel body (4 indented lines after the ExceptionType comment block)
# Lines 167-170 (0-based 166-169) are orphaned NONE/LOW/HIGH/CRITICAL without class header
to_remove = set()
for i in range(165, 172):
    stripped = lines[i].strip()
    if stripped in ('NONE = "NONE"', 'LOW = "LOW"', 'HIGH = "HIGH"', 'CRITICAL = "CRITICAL"'):
        to_remove.add(i)
        print(f"  Removing orphan line {i+1}: {stripped}")

# 2. Remove old duplicate rule classes and helpers (class ValidationResult through def parse_decimal)
# These are at lines 242-607 (0-based 241-606)
for i in range(240, 610):
    stripped = lines[i].strip()
    if i < len(lines):
        # class definitions
        if stripped.startswith('class ValidationResult') or \
           stripped.startswith('class OrderContext') or \
           stripped.startswith('class OrderValidationRule') or \
           stripped.startswith('class RequiredHeadFieldsRule') or \
           stripped.startswith('class PhaseOneCompletenessRule') or \
           stripped.startswith('class CustomerMappingRule') or \
           stripped.startswith('class PositiveAmountRule') or \
           stripped.startswith('class AmountConsistencyRule') or \
           stripped.startswith('class HasOrderItemsRule') or \
           stripped.startswith('class KnownSkuRule') or \
           stripped.startswith('class LocalInventoryAvailableRule'):
            print(f"  Removing class at line {i+1}: {stripped[:60]}")
            to_remove.add(i)
        # DEFAULT_RULES
        if stripped.startswith('DEFAULT_RULES'):
            print(f"  Removing DEFAULT_RULES at line {i+1}")
            to_remove.add(i)
        # Old helper functions
        if stripped.startswith('def config_value(') or \
           stripped.startswith('def config_bool(') or \
           stripped.startswith('def config_list(') or \
           stripped.startswith('def config_dict(') or \
           stripped.startswith('def config_int(') or \
           stripped.startswith('def is_approved_status(') or \
           stripped.startswith('def inventory_available_quantity(') or \
           stripped.startswith('def parse_decimal('):
            print(f"  Removing def at line {i+1}: {stripped[:60]}")
            to_remove.add(i)

# Now remove lines that are class/function bodies
# We need to find the extent of each class/def body
body_ranges = []
for start_line in sorted(to_remove):
    stripped = lines[start_line].strip()
    if stripped.startswith('class ') or stripped.startswith('def ') or stripped.startswith('DEFAULT_RULES'):
        # Find the end of this body: next class/def at same indent, or blank line + new class
        body_end = start_line
        for j in range(start_line + 1, len(lines)):
            if lines[j].strip() and not lines[j].startswith((' ', '\t', '#', '@', ')')):
                # This could be a new top-level statement
                # Check if it's the start of a new class/def
                if lines[j].strip().startswith(('class ', 'def ', 'STATE_TRANSITIONS', '@app.')):
                    body_end = j
                    break
            body_end = j
        body_ranges.append((start_line, body_end))
        print(f"  Body of {stripped[:50]}: lines {start_line+1}-{body_end+1}")

# Build cleaned lines
keep = [True] * len(lines)
for start, end in body_ranges:
    for i in range(start, end):
        keep[i] = False
# Also remove orphan lines
for i in to_remove:
    if i not in [s for s, e in body_ranges]:  # already covered
        keep[i] = False

# Also remove trailing blank lines between bodies
# After removing, collapse multiple blank lines
cleaned = []
prev_blank = False
for i, line in enumerate(lines):
    if keep[i]:
        is_blank = not line.strip()
        if is_blank and prev_blank:
            continue
        prev_blank = is_blank
        cleaned.append(line)

open(path, 'w').writelines(cleaned)
print(f"\n  Done. Original {len(lines)} lines → {len(cleaned)} lines.")
print(f"  Removed {len(lines) - len(cleaned)} lines of duplicate code.")
