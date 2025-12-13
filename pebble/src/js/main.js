/**
 * PTV Notify - Pebble.js Entry Point
 * 
 * This is the main entry point that initializes the Pebble.js framework
 * and loads the application.
 */

// Load Pebble.js modules
require('ui/simply-pebble').init();

// Load settings module first
require('settings');

// Load the main application
require('./app');
