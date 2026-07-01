const express = require('express');
const path = require('path');

const app = express();
const PORT = process.env.PORT || 3000;

// Serve static files from the 'public' directory
app.use(express.static(path.join(__dirname, 'public')));

// API endpoint to securely provide config from environment variables
app.get('/api/config', (req, res) => {
  res.json({
    sheetsApiUrl: process.env.SHEETS_API_URL || ''
  });
});

// Route for /code - serve code.html
app.get('/code', (req, res) => {
  res.sendFile(path.join(__dirname, 'public', 'code.html'));
});

// Route for /prop-firm - serve prop-firm.html
app.get('/prop-firm', (req, res) => {
  res.sendFile(path.join(__dirname, 'public', 'prop-firm.html'));
});

// All other routes go to index.html
app.get('*', (req, res) => {
  res.sendFile(path.join(__dirname, 'public', 'index.html'));
});

app.listen(PORT, () => {
  console.log(`CTC Strategy Website running at http://localhost:${PORT}`);
});
