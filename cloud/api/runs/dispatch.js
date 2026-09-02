import { endpoint, jsonBody } from "../../lib/http.js";
import { dispatch } from "../../lib/service.js";

export default endpoint({ methods: ["POST"], authenticated: true }, async (request, token) => {
  const body = await jsonBody(request);
  return dispatch(token, body.request, body.encrypted_secrets);
});
