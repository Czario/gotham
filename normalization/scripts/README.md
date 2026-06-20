# MongoDB Index Management

This directory contains scripts for managing MongoDB indexes to optimize performance.

## Quick Start

### Option 1: Automatic (Recommended)

Indexes are **automatically created** on application startup by default. No manual action needed!

The `DatabaseTracker` will check and create missing indexes when initialized.

### Option 2: Manual Creation

If you prefer to create indexes manually or need to troubleshoot:

```bash
# Using uv (recommended)
uv run python scripts/create_indexes.py

# Or using standard Python
python scripts/create_indexes.py
```

## What Indexes Are Created?

### Source Database (`financial_statements` collection)

1. **`idx_company_cik`** - Fast company lookups
   - Fields: `company_cik`
   
2. **`idx_company_cik_financial_data`** - Filter by company with data
   - Fields: `company_cik`, `financial_data`
   
3. **`idx_company_period_type`** - Fast statement lookups
   - Fields: `company_cik`, `reporting_period.end_date`, `statement_type`

### Target Database

#### `concept_values_annual` collection

1. **`idx_company_period_type`** - Fast processed statement checks
   - Fields: `company_cik`, `reporting_period.end_date`, `statement_type`
   
2. **`idx_company_cik`** - Fast company lookups
   - Fields: `company_cik`
   
3. **`idx_concept`** - Fast concept lookups
   - Fields: `concept`

#### `concept_values_quarterly` collection

Same indexes as annual collection.

#### `normalized_concepts_annual` & `normalized_concepts_quarterly` collections

1. **`idx_company_statement_concept`** - Fast concept lookups
   - Fields: `company_cik`, `statement_type`, `concept`, `dimension_concept`
   
2. **`idx_company_cik`** - Fast company lookups
   - Fields: `company_cik`
   
3. **`idx_concept`** - Fast concept lookups
   - Fields: `concept`

## Performance Impact

### Without Indexes (thousands of companies)
- 🐌 Startup: 30+ minutes
- 🐌 Company processing: 1-5 minutes each
- ⚠️ Risk of out-of-memory errors

### With Indexes (thousands of companies)
- ⚡ Startup: 1-5 seconds
- ⚡ Company processing: < 1 second each
- ✅ Scales to 100K+ companies

## Checking Existing Indexes

To view existing indexes in MongoDB:

```javascript
// In MongoDB shell or Compass

// Source database
use your_source_database;
db.financial_statements.getIndexes();

// Target database
use your_target_database;
db.concept_values_annual.getIndexes();
db.concept_values_quarterly.getIndexes();
db.normalized_concepts_annual.getIndexes();
db.normalized_concepts_quarterly.getIndexes();
```

## Disabling Automatic Index Creation

If you want to manage indexes manually and disable automatic creation:

```python
# In your code
tracker = DatabaseTracker(source_config, target_config, ensure_indexes=False)
```

## Troubleshooting

### "Could not ensure indexes" warning

If you see this warning on startup:
1. Check MongoDB connection permissions
2. Ensure your MongoDB user has index creation permissions
3. Run the manual script: `uv run python scripts/create_indexes.py`

### Slow queries after index creation

- Indexes may take time to build on large collections
- MongoDB builds indexes in the background by default
- Check index build status: `db.currentOp()` in MongoDB shell

### Too many indexes

Don't worry! MongoDB handles multiple indexes efficiently. The indexes created are carefully chosen to:
- Support the most common query patterns
- Not duplicate each other unnecessarily
- Provide optimal performance for the normalization process

## Notes

- **Idempotent**: Running index creation multiple times is safe
- **Background builds**: MongoDB builds indexes in the background
- **No downtime**: Indexes can be created while the system is running
- **Size**: Indexes typically use 5-10% of collection size
