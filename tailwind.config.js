/** Tailwind build config — rebuild static/css/app.css with:
 *  tailwindcss -c tailwind.config.js -i static/css/input.css -o static/css/app.css --minify
 */
module.exports = {
  content: ["./templates/**/*.html", "./*/templates/**/*.html"],
  theme: { extend: {} },
  plugins: [],
};
