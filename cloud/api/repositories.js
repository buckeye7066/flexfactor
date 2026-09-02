import { endpoint } from "../lib/http.js";
import { repositories } from "../lib/service.js";

export default endpoint({ methods: ["GET"], authenticated: true }, async (request, token) =>
  repositories(token, request.query?.page || 1));
