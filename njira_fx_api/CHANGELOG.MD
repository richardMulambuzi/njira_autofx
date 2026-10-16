# **Changelog**

All notable changes to this project will be documented in this file.

## **[Unreleased]**
- Future updates and features will be listed here.

---

## **[1.1.1] - 2025-01-04**

### **Removed**
- Removed **Swagger UI** integration due to project restructuring.

### **Added**
- Introduced **Dual Timeframe Moving Average & ATR Strategy** and **Malaysian Forex Strategy** for enhanced forex analysis. Users can now choose either strategy or use both for optimized trading.
  f
### **Changed**
- Refined the analysis module to allow the user to select a strategy and run the analysis based on the selected strategy.
- Improved the automated analysis functionality: if no `start_time` is provided, the analysis will default to the last 24 hours of data.
- Updated task scheduling for periodic forex data analysis, now running every 24 hours automatically.

---

## **[1.1.0] - 2024-12-29**

### **Added**
- Integrated **Swagger UI** into the project for easy testing of API endpoints. The Swagger documentation is accessible at [http://127.0.0.1:8000/swagger/](http://127.0.0.1:8000/swagger/).
- Added code for **analysis with duty forex data**, enabling deeper insights into forex trading trends and patterns.
- Implemented the **Maresian Strategy**, a custom trading strategy designed to analyze and optimize forex trading decisions.

### **Changed**
- Updated `settings.py` to include configurations for Swagger and static file handling.
- Enhanced middleware setup to support CORS and efficient static file management using `WhiteNoise`.

---

## **[1.0.0] - Initial Release**

### **Added**
- Basic Django project structure with initial setup.
- User authorization endpoint (`/authorize/`) for secure access control.
- SQLite database integration for lightweight development.

---

### **Versioning**
This project adheres to [Semantic Versioning](https://semver.org/). Changes are categorized as:
- **Added**: New features or functionality.
- **Changed**: Modifications to existing features.
- **Fixed**: Bug fixes or patches.
