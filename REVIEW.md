# REVIEW.md

This review covers the supplied starter `app/main.py` against
`openapi.yaml`, `DATA_MODEL.md`, and the attendance rules R1--R10.

## Defects found

  ----------------------------------------------------------------------------------------------------------------------
                 \# Where                    What is wrong                How to notice it  How it is addressed in the
                                                                                            implementation
  ----------------- ------------------------ ---------------------------- ----------------- ----------------------------
                  1 MongoDB client / module  `MongoClient` is created     Start the API     Read `MONGO_URI` and
                    initialization           while the module is          with an invalid   `MONGO_DB` from the
                                             imported, but the app does   or missing        environment, initialize
                                             not verify database          MongoDB           application resources
                                             connectivity during startup. configuration;    through FastAPI lifespan,
                                             The fallback URI can also    startup can       and ping MongoDB at startup.
                                             hide a missing deployment    appear successful 
                                             configuration.               even though       
                                                                          database          
                                                                          operations will   
                                                                          fail later.       

                  2 MongoDB initialization / The starter creates no       Inspect           Create the required indexes
                    indexes                  application indexes. This    `getIndexes()` or at startup, including unique
                                             allows duplicate business    run the required  employee-code and
                                             keys and makes the required  query-plan checks employee/date constraints
                                             list and analytics queries   against the       plus indexes supporting the
                                             harder to execute            seeded dataset.   documented filters and
                                             efficiently.                                   ordering.

                  3 `create_employee`        Duplicate detection is a     Send concurrent   Enforce uniqueness in
                                             check-then-insert sequence.  create requests   MongoDB and translate
                                             Two concurrent requests can  with the same     duplicate-key errors into
                                             both pass `find_one()` and   employee code.    `409 Conflict`.
                                             insert the same `emp_code`.                    

                  4 `create_employee` /      Input validation is too      Submit malformed  Validate request fields and
                    `EmployeeIn`             weak: date and shift strings `joined_on`,      date/time formats before
                                             are not checked against the  invalid shift     writing documents.
                                             contract, and field-level    times, or invalid 
                                             business constraints are not field values.     
                                             enforced.                                      

                  5 `create_employee`        `created_at` uses            Run the API on a  Store instants consistently
                                             `datetime.now()`, which      host with a       in UTC and convert them to
                                             produces a naive             different local   epoch milliseconds at the
                                             machine-local timestamp      timezone and      API boundary.
                                             rather than a consistent UTC inspect the       
                                             instant.                     stored timestamp. 

                  6 `list_employees`         Pagination uses              With 25 employees Use `(page - 1) * page_size`
                                             `skip = page * page_size`,   and               and validate that page is at
                                             so page 1 skips the first    `page_size=20`,   least 1 and page size is
                                             page of results.             page 1 starts     within the contract limit.
                                                                          after the first   
                                                                          20 employees.     

                  7 `list_employees`         `total` counts all           Request a         Count using the same filter
                                             employees, even when a       department filter as the list query and sort
                                             department filter is         when employees    by `emp_code` ascending
                                             applied. Results also lack   exist in multiple before applying pagination.
                                             the required deterministic   departments;      
                                             `emp_code` ascending sort.   compare `total`   
                                                                          with the filtered 
                                                                          result count.     

                  8 `list_employees`         There is no upper bound on   Request an        Apply the contract's
                                             `page_size`, so a client can extremely large   pagination validation,
                                             request an unnecessarily     page size.        including a maximum page
                                             large response.                                size of 100.

                  9 `health`                 The health endpoint always   Stop MongoDB      Check database readiness or
                                             returns `ok` and does not    while the API     distinguish process liveness
                                             check whether MongoDB is     process remains   from database readiness.
                                             reachable.                   running and       
                                                                          request           
                                                                          `/health`.        

                 10 `compute_late_minutes`   The calculation anchors      Test an           Determine the attendance
                                             shift start to the punch-in  overnight-shift   date and shift start using
                                             calendar date. It does not   employee punching the R1 IST/overnight rules,
                                             account for the              in after          then apply the strict
                                             attendance-day rules for     midnight, or send 10-minute grace rule and
                                             overnight shifts, and its    a timestamp whose floor elapsed minutes.
                                             correctness depends on the   UTC date differs  
                                             timestamp already being      from its IST      
                                             interpreted in IST.          date.             

                 11 `compute_work_hours`     Python's built-in `round()`  Use a duration    Apply explicit half-up
                                             does not guarantee the       whose two-decimal rounding for work hours as
                                             contract's decimal           result is exactly required by R4.
                                             `ROUND_HALF_UP` behavior for at a rounding     
                                             halfway cases.               midpoint.         

                 12 `compute_overtime`       Overtime is calculated only  Test a punch-out  Compute the correct
                                             against an end time on       after midnight    shift-end instant for the
                                             `date_str`, so it does not   for an overnight  attendance day and return
                                             handle overnight shifts      shift, and a      floored overtime minutes
                                             correctly. It also counts    punch-out 10      only when the threshold is
                                             any positive overtime, even  minutes after     met.
                                             though R3 requires at least  shift end.        
                                             30 minutes before overtime                     
                                             is reported.                                   

                 13 `punch_in`               The employee lookup result   Punch in with an  Return `404 Not Found` when
                                             is not checked before        `emp_code` that   the employee does not exist.
                                             `emp["shift_start"]` is      does not exist.   
                                             accessed. An unknown                           
                                             employee can cause an                          
                                             unhandled server error                         
                                             instead of the required                        
                                             response.                                      

                 14 `punch_in`               Epoch milliseconds are       Send a timestamp  Normalize input instants to
                                             converted with               near midnight UTC UTC, derive the attendance
                                             `datetime.fromtimestamp()`   or one containing date in IST according to R1,
                                             using the host's local       milliseconds and  and truncate instants to
                                             timezone, and the current    compare the       whole seconds before
                                             time uses naive              stored date and   calculations and storage.
                                             `datetime.now()`. This can   returned instant. 
                                             assign the wrong IST                           
                                             attendance date. Timestamps                    
                                             are also not consistently                      
                                             truncated to whole seconds                     
                                             as required.                                   

                 15 `punch_in`               The duplicate-record check   Send multiple     Use the unique
                                             followed by insert is not    simultaneous      `(emp_code, date)` database
                                             atomic. Concurrent requests  punch-in requests index as the concurrency
                                             can both observe no record   for the same      guard and map duplicate-key
                                             and attempt insertion.       employee and      errors to `409 Conflict`.
                                                                          attendance date.  

                 16 `punch_in` / `PunchInIn` The status is an             Submit a status   Restrict punch-in to
                                             unrestricted string, so      such as `ABSENT`  `PRESENT`, `WFH`, and
                                             unsupported statuses can be  or an arbitrary   `ON_DUTY`; reserve `ABSENT`
                                             written through punch-in.    string to         and `LEAVE` for
                                             The endpoint also does not   punch-in.         existing/seeded data or
                                             enforce the contract's                         regularization as specified.
                                             allowed punch-in statuses.                     

                 17 `punch_in`               The attendance date is taken Test an overnight Compute the attendance date
                                             directly from the            shift with a      from the employee's shift
                                             timestamp's local date       punch-in after    and the timestamp in IST.
                                             rather than applying R1's    midnight but      
                                             IST calendar-date and        before the shift  
                                             overnight-shift rules.       end.              

                 18 `list_attendance`        The endpoint loads every     Run the endpoint  Apply filters, deterministic
                                             matching document into       against a large   sort, skip, and limit in
                                             application memory, sorts in collection and    MongoDB; use indexes that
                                             Python, then slices the      inspect memory    support the query patterns.
                                             requested page. This does    use and query     
                                             not scale to the             execution.        
                                             assignment's 100,000-record                    
                                             dataset.                                       

                 19 `list_attendance`        Sorting uses only `date`     Create multiple   Sort by `date` descending
                                             descending. Records with the attendance        and `emp_code` ascending in
                                             same date have no            records for the   MongoDB.
                                             deterministic secondary      same date and     
                                             order, despite the contract  request the list  
                                             requiring `emp_code`         repeatedly.       
                                             ascending.                                     

                 20 `list_attendance`        Date filters are treated as  Submit malformed  Validate dates and
                                             unchecked strings, and       dates,            pagination and return
                                             reversed date ranges are not `date_from` later `422 Unprocessable Entity`
                                             rejected. Pagination values  than `date_to`,   for invalid parameters,
                                             are also not validated.      page 0, or an     including an inverted range.
                                                                          excessive page    
                                                                          size.             

                 21 `list_attendance`        The implementation does not  Inspect the query Exclude `_id` in the
                                             explicitly exclude MongoDB's projection and    projection and serialize
                                             internal `_id` field in its  response          only documented response
                                             query. It removes `_id` only conversion for    fields, converting instants
                                             for returned page documents, documents         to epoch milliseconds.
                                             which is unnecessary work    containing        
                                             and makes the response       ObjectIds.        
                                             conversion more fragile.                       

                 22 `app/main.py`            The starter does not         Request any of    Implement each required
                                             implement punch-out,         the listed paths; endpoint and its documented
                                             regularization, monthly      they are absent   status codes, calculations,
                                             employee analytics,          from the starter. aggregation behavior, and
                                             department summary, late                       query parameters.
                                             leaderboard, department                        
                                             trend, or query explain                        
                                             endpoints required by the                      
                                             contract.                                      

                 23 `app/main.py` /          The starter contains no      Inspect the       Use MongoDB aggregation
                    analytics                aggregation pipelines, so it implementation or pipelines for the three
                                             cannot meet the requirement  call the required analytics operations and
                                             to perform analytics in      analytics         verify query plans with
                                             MongoDB or demonstrate index endpoints.        execution statistics.
                                             use for the analytics                          
                                             endpoints.                                     
  ----------------------------------------------------------------------------------------------------------------------

## Reviewed and not considered defects

-   **`shift_end <= shift_start` is not invalid by itself.**
    `DATA_MODEL.md` and R1 define this as an overnight shift, so the
    implementation must support it rather than reject it.
-   **`emp_code` is the public employee identifier, not MongoDB `_id`.**
    The data model explicitly requires this, so using `emp_code` in API
    routes and attendance references is correct.
-   **Attendance `date` and `joined_on` are strings by design.** The
    data model distinguishes calendar dates from instants; they should
    not be converted to timestamps just to store them.
-   **A weekday with no attendance document is not automatically an
    `ABSENT` record.** The data model explicitly distinguishes missing
    logs from an `ABSENT` status.
-   **The starter's use of synchronous PyMongo is not inherently a
    defect.** It can be used with FastAPI, provided database operations
    and lifecycle handling are implemented consistently.
