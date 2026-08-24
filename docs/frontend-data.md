# frontend-data.md

## Page Data Specifications

### Landing Page
- **Data Fields**: Featured services, recent metrics, upcoming promotions
- **Purpose**: Overview of platform capabilities

### Login Page
- **Fields**:
  - username (required)
  - password (required)
- **Validation**:
  - Username/password format checks
  - Rate-limited to prevent brute force
- **Default Admin Account**:
  - `username`: `Darshan`
  - `password`: `Darsh1812`
  - `role`: `admin` (full permissions)

### Register Page
- **Fields**:
  - username (required, unique)
  - email (required, validated format)
  - password (required, min 8 chars)
  - password confirmation
- **Validation**:
  - Email uniqueness check
  - Password strength requirements

### Dashboard
- **Data**:
  - User profile summary
  - Recent readings
  - Alert notifications
  - Historical trends

### Usersettings
- **Sections**:
  - Personal info (name, email, role)
  - Notification preferences
  - Display themes

### History
- **Content**:
  - Reading history
  - Alert logs
  - API request history

### Other Pages
- **To-Do**: Specify additional pages once defined

## API Reference

### Login Endpoint
`POST /api/auth/login`
- Returns: JWT tokens, user profile

### Register Endpoint
`POST /api/auth/register`
- Returns: JWT tokens, user confirmation status