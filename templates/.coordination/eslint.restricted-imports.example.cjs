/**
 * Example ESLint config fragment for module boundaries.
 * Adapt paths to your repo; integrate into eslint.config.js / .eslintrc.
 */
module.exports = {
  rules: {
    "import/no-restricted-paths": [
      "error",
      {
        zones: [
          {
            target: "./src/modules/payments",
            from: "./src/modules/auth",
            except: ["./src/modules/auth/api.ts"],
            message: "Payments must not import auth internals; use auth/api.ts",
          },
        ],
      },
    ],
  },
};
