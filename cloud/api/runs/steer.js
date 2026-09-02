import { endpoint, jsonBody } from "../../lib/http.js";
import { submitSteering } from "../../lib/service.js";

export default endpoint({ methods: ["POST"], authenticated: true }, async (request, token) => {
  const body = await jsonBody(request);
  return submitSteering(token, body.repository, body.request_id, body.comment);
});
