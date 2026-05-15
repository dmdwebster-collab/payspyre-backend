# Analytics API Documentation

The Analytics API provides comprehensive insights into lending platform performance through aggregated data queries.

## Endpoints

### GET /api/v1/analytics

Retrieves comprehensive analytics data for the lending platform.

**Query Parameters:**
- `start_date` (optional): Start date in YYYY-MM-DD format. Default: 90 days ago
- `end_date` (optional): End date in YYYY-MM-DD format. Default: today
- `granularity` (optional): Time granularity - "daily", "weekly", or "monthly". Default: "daily"

**Response:**

```json
{
  "loan_volume_trends": [
    {
      "date": "2024-01-01",
      "volume": 125000.00,
      "count": 25
    }
  ],
  "approval_rates": [
    {
      "vendorId": "uuid",
      "vendorName": "Vendor Name",
      "submitted": 100,
      "approved": 75,
      "rejected": 25,
      "approvalRate": 0.75
    }
  ],
  "loan_metrics": {
    "totalVolume": 8500000.00,
    "totalCount": 1425,
    "averageAmount": 5964.91,
    "averageTerm": 18.5
  },
  "payment_collections": [
    {
      "period": "2024-01",
      "scheduled": 150,
      "collected": 135,
      "collectionRate": 0.90,
      "amountScheduled": 75000.00,
      "amountCollected": 67500.00
    }
  ],
  "delinquency_tracking": [
    {
      "period": "2024-01",
      "totalActive": 200,
      "current": 140,
      "days1to30": 30,
      "days31to60": 16,
      "days61to90": 8,
      "days90Plus": 6,
      "delinquencyRate": 0.30
    }
  ],
  "risk_score_distribution": [
    {
      "scoreRange": "750+ (Excellent)",
      "count": 425,
      "percentage": 0.298
    }
  ],
  "vendor_performance": [
    {
      "vendorId": "uuid",
      "vendorName": "Vendor Name",
      "loanCount": 285,
      "totalVolume": 1500000.00,
      "averageLoanAmount": 5263.16,
      "approvalRate": 0.78,
      "collectionRate": 0.92,
      "delinquencyRate": 0.05,
      "rank": 1
    }
  ],
  "geographic_distribution": [
    {
      "province": "BC",
      "loanCount": 641,
      "totalVolume": 3800000.00,
      "averageLoanAmount": 5928.24,
      "percentage": 0.45
    }
  ]
}
```

### GET /api/v1/analytics/export

Exports analytics data to CSV format.

**Query Parameters:**
- `start_date` (optional): Start date in YYYY-MM-DD format. Default: 90 days ago
- `end_date` (optional): End date in YYYY-MM-DD format. Default: today
- `type` (optional): Export type - "loans", "payments", or "vendors". Default: "loans"

**Response:** CSV file download

## Data Models

### Loan Volume Trends
Tracks loan volume over time based on approved/funded loans.

**Fields:**
- `date`: Period identifier (date string based on granularity)
- `volume`: Total loan amount for the period
- `count`: Number of loans in the period

### Approval Rates
Shows approval/rejection breakdown by vendor.

**Fields:**
- `vendorId`: Vendor UUID
- `vendorName`: Vendor business name
- `submitted`: Total applications submitted
- `approved`: Number of approved applications
- `rejected`: Number of rejected applications
- `approvalRate`: Calculated approval rate (0-1)

### Loan Metrics
Overall loan statistics for the period.

**Fields:**
- `totalVolume`: Sum of all loan amounts
- `totalCount`: Total number of loans
- `averageAmount`: Mean loan amount
- `averageTerm`: Mean loan term in months

### Payment Collections
Payment collection performance over time.

**Fields:**
- `period`: Period identifier
- `scheduled`: Number of scheduled payments
- `collected`: Number of completed payments
- `collectionRate`: Completion rate (0-1)
- `amountScheduled`: Total amount scheduled
- `amountCollected`: Total amount collected

### Delinquency Tracking
Loan delinquency categorized by days past due.

**Fields:**
- `period`: Period identifier
- `totalActive`: Total active payment schedules
- `current`: Not past due
- `days1to30`: 1-30 days past due
- `days31to60`: 31-60 days past due
- `days61to90`: 61-90 days past due
- `days90Plus`: 90+ days past due
- `delinquencyRate`: Overall delinquency rate (0-1)

### Risk Score Distribution
Borrower credit score breakdown.

**Fields:**
- `scoreRange`: Credit score bucket (e.g., "750+ (Excellent)")
- `count`: Number of loans in bucket
- `percentage`: Percentage of total loans

### Vendor Performance
Comprehensive vendor performance metrics.

**Fields:**
- `vendorId`: Vendor UUID
- `vendorName`: Vendor business name
- `loanCount`: Total loans
- `totalVolume`: Total loan volume
- `averageLoanAmount`: Mean loan amount
- `approvalRate`: Approval rate (0-1)
- `collectionRate`: Payment collection rate (0-1)
- `delinquencyRate`: Delinquency rate (0-1)
- `rank`: Performance rank (1 = best)

**Ranking Formula:**
```
Performance Score = (Volume × 0.4) +
                    (Approval Rate × Loan Count × 0.3) +
                    (Collection Rate × Loan Count × 0.2) +
                    ((1 - Delinquency Rate) × Loan Count × 0.1)
```

### Geographic Distribution
Loan distribution by Canadian province/territory.

**Fields:**
- `province`: Province code (e.g., "BC", "ON")
- `loanCount`: Number of loans
- `totalVolume`: Total loan volume
- `averageLoanAmount`: Mean loan amount
- `percentage`: Percentage of total loans

## Database Queries

The analytics endpoint uses optimized SQL queries with:
- Date truncation for time-based aggregation
- CASE statements for conditional counting
- JOIN operations for related data
- Window functions for ranking
- COALESCE for handling NULL values

## Performance Considerations

1. **Indexing**: Ensure proper indexes exist on:
   - `loan_applications.created_at`
   - `loan_applications.vendor_id`
   - `loan_applications.borrower_id`
   - `loan_applications.status`
   - `payments.payment_date`
   - `payments.status`
   - `payment_schedule.due_date`
   - `borrowers.province`
   - `borrowers.credit_score`

2. **Query Optimization**:
   - Date range filtering is applied first
   - Aggregations use database functions for efficiency
   - Subqueries are used for complex joins

3. **Caching**: Consider caching responses for identical date ranges

## Testing

The analytics endpoint includes comprehensive test cases in `tests/test_analytics.py`.

## Security

- Requires database connection
- No authentication required for internal use
- Add authentication middleware for production
- Consider rate limiting for public access

## Future Enhancements

- Real-time WebSocket updates
- Custom date range presets
- Drill-down capabilities
- Comparative period analysis
- Export to multiple formats (PDF, Excel)
- Custom dashboard configurations