import { endpoint, jsonBody } from "../lib/http.js";
import { configure } from "../lib/service.js";

export default endpoint({ methods: ["POST"], authenticated: true }, async (request, token) => {
  await jsonBody(request);
  return configure(token);
});
